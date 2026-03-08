"""
modules/turnos/tests/test_permissions.py

Tests for the turnos permission layer.

Strategy:
    - Create users with specific permission sets using make_usuario_con_permisos()
    - Test both has_permission (action-level) and has_object_permission (object-level)
    - Verify that profesionales only see their own turnos
    - Verify that recepcion can see all turnos
    - Verify that admins bypass all granular checks
    - Verify that 403 is returned for users without the required permission

Each test builds its own fixtures — no shared state.
"""

from datetime import timedelta

from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.turnos.api.permissions import (
    PERM_VER, PERM_CREAR, PERM_CONFIRMAR,
    PERM_CANCELAR, PERM_REPROGRAMAR, PERM_COMPLETAR,
    PuedeVerTurnos,
    PuedeCrearTurnos,
    PuedeCompletarTurnos,
    TurnoObjectPermission,
    _get_profesional_del_usuario,
    _es_admin,
    _tiene_permiso,
)
from modules.turnos.models import ActorCancelacion, EstadoTurno
from modules.turnos.services import TurnoService
from modules.turnos.tests.factories import (
    activar_modulo,
    asignar_permiso,
    make_admin,
    make_empresa,
    make_profesional,
    make_profesional_servicio,
    make_servicio,
    make_turno,
    make_usuario,
    make_usuario_con_permisos,
    setup_turno_completo,
)


def tomorrow_at(hour: int, minute: int = 0):
    from datetime import date, datetime
    tomorrow = date.today() + timedelta(days=1)
    dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute)
    return timezone.make_aware(dt)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — permission helpers
# ─────────────────────────────────────────────────────────────────────────────

class EsAdminHelperTest(TestCase):

    def setUp(self):
        self.empresa = make_empresa()

    def test_admin_devuelve_true(self):
        admin = make_admin(self.empresa)
        self.assertTrue(_es_admin(admin))

    def test_usuario_normal_devuelve_false(self):
        usuario = make_usuario(self.empresa)
        self.assertFalse(_es_admin(usuario))


class TienePermisoHelperTest(TestCase):

    def setUp(self):
        self.empresa = make_empresa()

    def test_usuario_con_permiso_devuelve_true(self):
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        self.assertTrue(_tiene_permiso(usuario, PERM_VER))

    def test_usuario_sin_permiso_devuelve_false(self):
        usuario = make_usuario(self.empresa)
        self.assertFalse(_tiene_permiso(usuario, PERM_VER))

    def test_permiso_distinto_devuelve_false(self):
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        self.assertFalse(_tiene_permiso(usuario, PERM_CREAR))


class GetProfesionalDelUsuarioTest(TestCase):
    """Tests for the request-level profesional resolver."""

    def setUp(self):
        self.empresa = make_empresa()
        self.factory = RequestFactory()

    def _make_request(self, usuario):
        request = self.factory.get("/")
        request.user    = usuario
        request.empresa = self.empresa
        return request

    def test_usuario_con_profesional_lo_resuelve(self):
        """A user linked to a Profesional record returns that record."""
        usuario     = make_usuario(self.empresa)
        profesional = make_profesional(self.empresa, usuario=usuario)

        request = self._make_request(usuario)
        resultado = _get_profesional_del_usuario(request)

        self.assertIsNotNone(resultado)
        self.assertEqual(resultado.id, profesional.id)

    def test_usuario_sin_profesional_devuelve_none(self):
        """A user without a Profesional record returns None."""
        usuario = make_usuario(self.empresa)  # no profesional linked
        request = self._make_request(usuario)
        self.assertIsNone(_get_profesional_del_usuario(request))

    def test_resultado_se_cachea_en_request(self):
        """The profesional lookup is cached on the request object."""
        usuario     = make_usuario(self.empresa)
        make_profesional(self.empresa, usuario=usuario)
        request = self._make_request(usuario)

        # First call — hits DB
        r1 = _get_profesional_del_usuario(request)
        # Second call — must use cache (same object identity)
        r2 = _get_profesional_del_usuario(request)

        self.assertIs(r1, r2)
        self.assertTrue(hasattr(request, "_profesional_cache"))


# ─────────────────────────────────────────────────────────────────────────────
# Permission class unit tests (using mock request objects)
# ─────────────────────────────────────────────────────────────────────────────

class PermissionClassUnitTest(TestCase):
    """
    Test has_permission() directly without going through the HTTP stack.
    Uses a minimal mock-like request built with RequestFactory.
    """

    def setUp(self):
        self.empresa = make_empresa()
        self.factory = RequestFactory()

    def _make_request(self, usuario):
        request = self.factory.get("/")
        request.user    = usuario
        request.empresa = self.empresa
        return request

    def test_admin_pasa_cualquier_permiso(self):
        admin   = make_admin(self.empresa)
        request = self._make_request(admin)

        for perm_class in [PuedeVerTurnos(), PuedeCrearTurnos(), PuedeCompletarTurnos()]:
            self.assertTrue(
                perm_class.has_permission(request, view=None),
                f"{perm_class.__class__.__name__} should pass for admin",
            )

    def test_usuario_con_permiso_pasa(self):
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        request = self._make_request(usuario)

        perm = PuedeVerTurnos()
        self.assertTrue(perm.has_permission(request, view=None))

    def test_usuario_sin_permiso_falla(self):
        usuario = make_usuario(self.empresa)  # no permisos
        request = self._make_request(usuario)

        perm = PuedeVerTurnos()
        self.assertFalse(perm.has_permission(request, view=None))

    def test_crear_requiere_permiso_crear_no_ver(self):
        """PERM_VER alone does not grant PERM_CREAR."""
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        request = self._make_request(usuario)

        perm = PuedeCrearTurnos()
        self.assertFalse(perm.has_permission(request, view=None))


# ─────────────────────────────────────────────────────────────────────────────
# Object-level permission unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TurnoObjectPermissionTest(TestCase):

    def setUp(self):
        self.empresa = make_empresa()
        self.servicio = make_servicio(self.empresa)
        self.factory = RequestFactory()

    def _make_request(self, usuario):
        request = self.factory.get("/")
        request.user    = usuario
        request.empresa = self.empresa
        return request

    def test_admin_accede_a_cualquier_turno(self):
        admin       = make_admin(self.empresa)
        profesional = make_profesional(self.empresa)
        turno       = make_turno(self.empresa, profesional, self.servicio)

        request = self._make_request(admin)
        perm    = TurnoObjectPermission()
        self.assertTrue(perm.has_object_permission(request, view=None, obj=turno))

    def test_profesional_accede_a_su_propio_turno(self):
        usuario     = make_usuario(self.empresa)
        profesional = make_profesional(self.empresa, usuario=usuario)
        turno       = make_turno(self.empresa, profesional, self.servicio)

        request = self._make_request(usuario)
        perm    = TurnoObjectPermission()
        self.assertTrue(perm.has_object_permission(request, view=None, obj=turno))

    def test_profesional_no_accede_a_turno_de_otro_profesional(self):
        usuario_a    = make_usuario(self.empresa)
        profesional_a = make_profesional(self.empresa, usuario=usuario_a)

        profesional_b = make_profesional(self.empresa)
        turno_b       = make_turno(self.empresa, profesional_b, self.servicio)

        request = self._make_request(usuario_a)
        perm    = TurnoObjectPermission()
        self.assertFalse(perm.has_object_permission(request, view=None, obj=turno_b))

    def test_recepcion_accede_a_cualquier_turno(self):
        """A user without a Profesional record (recepcion) can access all turnos."""
        recepcion   = make_usuario_con_permisos(self.empresa, [PERM_VER])
        profesional = make_profesional(self.empresa)
        turno       = make_turno(self.empresa, profesional, self.servicio)

        request = self._make_request(recepcion)
        perm    = TurnoObjectPermission()
        self.assertTrue(perm.has_object_permission(request, view=None, obj=turno))

    def test_tenant_mismatch_deniega_acceso(self):
        """A turno from another empresa is denied even for admins."""
        otra_empresa = make_empresa()
        prof_otra    = make_profesional(otra_empresa)
        serv_otra    = make_servicio(otra_empresa)
        turno_otro   = make_turno(otra_empresa, prof_otra, serv_otra)

        admin   = make_admin(self.empresa)
        request = self._make_request(admin)
        perm    = TurnoObjectPermission()
        self.assertFalse(perm.has_object_permission(request, view=None, obj=turno_otro))


# ─────────────────────────────────────────────────────────────────────────────
# API integration permission tests
# ─────────────────────────────────────────────────────────────────────────────

class PermisosAPITest(APITestCase):
    """
    End-to-end permission tests through the HTTP stack.

    These verify that the get_permissions() routing in TurnoViewSet
    correctly enforces the per-action permission requirements.
    """

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]
        activar_modulo(self.empresa, "turnos")

    def _authenticate(self, usuario):
        tokens = get_tokens_for_user(usuario)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def _crear_turno_via_service(self, hora=10):
        admin = make_admin(self.empresa)
        return TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(hora),
            usuario=admin,
        )

    def test_usuario_sin_permiso_recibe_403_en_list(self):
        """A user with no permissions gets 403 on GET /turnos/."""
        usuario = make_usuario(self.empresa)  # no permisos
        self._authenticate(usuario)

        response = self.client.get(reverse("turno-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_usuario_con_perm_ver_puede_listar(self):
        """A user with turnos.ver can GET /turnos/."""
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        self._authenticate(usuario)

        response = self.client.get(reverse("turno-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_usuario_sin_perm_crear_recibe_403_en_post(self):
        """A user with only turnos.ver gets 403 on POST /turnos/."""
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        self._authenticate(usuario)

        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10).isoformat(),
        }
        response = self.client.post(reverse("turno-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_recepcion_puede_crear_turnos(self):
        """A user with turnos.crear can POST /turnos/."""
        recepcion = make_usuario_con_permisos(self.empresa, [PERM_VER, PERM_CREAR])
        self._authenticate(recepcion)

        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10).isoformat(),
        }
        response = self.client.post(reverse("turno-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_profesional_solo_ve_sus_turnos(self):
        """
        A user linked to Profesional A can retrieve Profesional A's turno
        but gets 403 on a turno belonging to Profesional B.
        """
        # Profesional A has a platform user
        usuario_a    = make_usuario_con_permisos(self.empresa, [PERM_VER])
        profesional_a = make_profesional(self.empresa, usuario=usuario_a)
        make_profesional_servicio(self.empresa, profesional_a, self.servicio)
        from modules.turnos.tests.factories import make_horario
        from datetime import date
        make_horario(
            self.empresa, profesional_a,
            dia_semana=(date.today() + timedelta(days=1)).weekday(),
        )

        turno_a = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=profesional_a,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )
        # Profesional B's turno (already exists from setup_turno_completo)
        turno_b = self._crear_turno_via_service(hora=12)

        self._authenticate(usuario_a)

        # Can access own turno
        r_own = self.client.get(
            reverse("turno-detail", kwargs={"pk": str(turno_a.id)})
        )
        self.assertEqual(r_own.status_code, status.HTTP_200_OK)

        # Cannot access another professional's turno
        r_other = self.client.get(
            reverse("turno-detail", kwargs={"pk": str(turno_b.id)})
        )
        self.assertEqual(r_other.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_accede_a_todos_los_turnos(self):
        """An admin can access any turno within their empresa."""
        admin  = make_admin(self.empresa)
        turno  = self._crear_turno_via_service(hora=10)
        self._authenticate(admin)

        response = self.client.get(
            reverse("turno-detail", kwargs={"pk": str(turno.id)})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_usuario_sin_perm_completar_recibe_403(self):
        """A user with only turnos.ver cannot mark a turno as completado."""
        usuario = make_usuario_con_permisos(self.empresa, [PERM_VER])
        self._authenticate(usuario)

        turno = self._crear_turno_via_service(hora=10)
        admin = make_admin(self.empresa)
        TurnoService.confirmar_turno(turno, usuario=admin)
        turno.refresh_from_db()

        response = self.client.post(
            reverse("turno-completar", kwargs={"pk": str(turno.id)}),
            {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_usuario_otra_empresa_recibe_403_o_404(self):
        """A user from empresa B cannot access empresa A's turnos."""
        empresa_b = make_empresa()
        activar_modulo(empresa_b, "turnos")
        admin_b = make_admin(empresa_b)
        self._authenticate(admin_b)

        turno_a = self._crear_turno_via_service(hora=10)
        response = self.client.get(
            reverse("turno-detail", kwargs={"pk": str(turno_a.id)})
        )
        # IsTenantAuthenticated or TurnoObjectPermission will reject — 403 or 404
        self.assertIn(
            response.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
