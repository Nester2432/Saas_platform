"""
modules/turnos/tests/test_api_turnos.py

API integration tests for the turnos module.

Tests make real HTTP requests through the full Django/DRF stack:
    URL routing → permissions → TurnoViewSet → TurnoService → DB

Exercises:
    - TenantMiddleware (empresa resolved from JWT)
    - ModuloActivoPermission (module must be active for "turnos")
    - IsTenantAuthenticated (user must belong to empresa)
    - TurnoObjectPermission (object must belong to empresa)
    - Pagination envelope (count/next/previous/results)
    - All state-transition action endpoints
    - Slots endpoint with query params

Each test class has its own setUp — no shared DB state.
"""

from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.turnos.models import (
    ActorCancelacion,
    EstadoTurno,
    Turno,
)
from modules.turnos.services import TurnoService
from modules.turnos.tests.factories import (
    activar_modulo,
    asignar_permiso,
    make_admin,
    make_bloqueo,
    make_horario,
    make_profesional,
    make_profesional_servicio,
    make_servicio,
    make_turno,
    make_usuario,
    make_usuario_con_permisos,
    setup_turno_completo,
)
from modules.turnos.api.permissions import (
    PERM_VER, PERM_CREAR, PERM_CONFIRMAR,
    PERM_CANCELAR, PERM_REPROGRAMAR, PERM_COMPLETAR,
)


def tomorrow_at(hour: int, minute: int = 0):
    from datetime import datetime
    tomorrow = date.today() + timedelta(days=1)
    dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute)
    return timezone.make_aware(dt)


# ─────────────────────────────────────────────────────────────────────────────
# Base test case
# ─────────────────────────────────────────────────────────────────────────────

class TurnoAPITestCase(APITestCase):
    """
    Base class for turnos API tests.

    Provides:
        self.empresa     → active Empresa with "turnos" module active
        self.usuario     → admin user (is_empresa_admin=True)
        self.profesional → active Profesional
        self.servicio    → active Servicio (60 min)
        self.client      → APIClient with Bearer token

    setUp creates a complete scheduling graph so tests can immediately
    call TurnoService or the API without additional setup.
    """

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]
        self.usuario     = ctx["admin"]

        activar_modulo(self.empresa, "turnos")
        self._authenticate(self.usuario)

    def _authenticate(self, usuario):
        tokens = get_tokens_for_user(usuario)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def _url_list(self):
        return reverse("turno-list")

    def _url_detail(self, pk):
        return reverse("turno-detail", kwargs={"pk": str(pk)})

    def _url_action(self, action_name, pk):
        return reverse(f"turno-{action_name}", kwargs={"pk": str(pk)})

    def _crear_turno_via_service(self, hora=10):
        """Create a turno directly via service (bypasses HTTP for setup)."""
        return TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(hora),
            usuario=self.usuario,
        )


# ─────────────────────────────────────────────────────────────────────────────
# List & retrieve
# ─────────────────────────────────────────────────────────────────────────────

class TurnoListTest(TurnoAPITestCase):

    def test_listar_turnos_retorna_200(self):
        self._crear_turno_via_service()
        response = self.client.get(self._url_list())
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_respuesta_tiene_envelope_paginado(self):
        response = self.client.get(self._url_list())
        for key in ("count", "results", "next", "previous"):
            self.assertIn(key, response.data)

    def test_lista_solo_turnos_de_la_empresa(self):
        """Turnos from another empresa must never appear in the list."""
        self._crear_turno_via_service()

        from modules.turnos.tests.factories import make_empresa
        otra_empresa = make_empresa()
        activar_modulo(otra_empresa, "turnos")
        ctx_b = setup_turno_completo(empresa=otra_empresa)
        make_turno(otra_empresa, ctx_b["profesional"], ctx_b["servicio"])

        response = self.client.get(self._url_list())
        ids = [r["id"] for r in response.data["results"]]
        # All returned turnos belong to self.empresa
        for t in Turno.objects.filter(id__in=ids):
            self.assertEqual(str(t.empresa_id), str(self.empresa.id))

    def test_filtro_por_estado(self):
        """?estado=PENDIENTE returns only PENDIENTE turnos."""
        self._crear_turno_via_service(hora=10)

        turno2 = self._crear_turno_via_service(hora=12)
        TurnoService.confirmar_turno(turno2)

        response = self.client.get(self._url_list(), {"estado": "PENDIENTE"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for result in response.data["results"]:
            self.assertEqual(result["estado"], "PENDIENTE")

    def test_filtro_por_profesional(self):
        """?profesional=<id> returns only turnos for that professional."""
        prof2 = make_profesional(self.empresa)
        serv2 = make_servicio(self.empresa)
        make_profesional_servicio(self.empresa, prof2, serv2)
        from modules.turnos.tests.factories import make_horario
        make_horario(
            self.empresa, prof2,
            dia_semana=(date.today() + timedelta(days=1)).weekday(),
        )
        TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=prof2,
            servicio=serv2,
            fecha_inicio=tomorrow_at(10),
        )
        self._crear_turno_via_service(hora=11)

        response = self.client.get(
            self._url_list(), {"profesional": str(prof2.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for result in response.data["results"]:
            self.assertEqual(result["profesional"]["id"], str(prof2.id))


class TurnoRetrieveTest(TurnoAPITestCase):

    def test_retrieve_retorna_200(self):
        turno = self._crear_turno_via_service()
        response = self.client.get(self._url_detail(turno.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_retrieve_contiene_campos_esperados(self):
        turno = self._crear_turno_via_service()
        response = self.client.get(self._url_detail(turno.id))
        data = response.data
        for field in ("id", "estado", "fecha_inicio", "fecha_fin",
                      "profesional", "servicio", "duracion_minutos"):
            self.assertIn(field, data, f"Campo '{field}' no está en la respuesta")

    def test_retrieve_turno_otra_empresa_retorna_404(self):
        """A turno from another empresa must return 404 (tenant isolation)."""
        from modules.turnos.tests.factories import make_empresa
        otra = make_empresa()
        ctx  = setup_turno_completo(empresa=otra)
        t    = make_turno(otra, ctx["profesional"], ctx["servicio"])

        response = self.client.get(self._url_detail(t.id))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ─────────────────────────────────────────────────────────────────────────────
# Crear turno (POST /turnos/)
# ─────────────────────────────────────────────────────────────────────────────

class CrearTurnoAPITest(TurnoAPITestCase):

    def test_api_crear_turno_retorna_201(self):
        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10).isoformat(),
        }
        response = self.client.post(self._url_list(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_api_crear_turno_respuesta_contiene_id(self):
        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10).isoformat(),
        }
        response = self.client.post(self._url_list(), payload, format="json")
        self.assertIn("id", response.data)
        self.assertEqual(response.data["estado"], EstadoTurno.PENDIENTE)

    def test_api_crear_turno_fecha_pasada_retorna_400(self):
        """fecha_inicio in the past must be rejected by the serializer."""
        pasado = timezone.now() - timedelta(hours=1)
        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   pasado.isoformat(),
        }
        response = self.client.post(self._url_list(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_crear_turno_slot_ocupado_retorna_409(self):
        """Booking a taken slot returns 409 Conflict."""
        self._crear_turno_via_service(hora=10)

        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10, 30).isoformat(),
        }
        response = self.client.post(self._url_list(), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_api_crear_turno_sin_autenticacion_retorna_401(self):
        self.client.credentials()  # clear auth
        payload = {
            "profesional_id": str(self.profesional.id),
            "servicio_id":    str(self.servicio.id),
            "fecha_inicio":   tomorrow_at(10).isoformat(),
        }
        response = self.client.post(self._url_list(), payload, format="json")
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))


# ─────────────────────────────────────────────────────────────────────────────
# Confirmar turno
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmarTurnoAPITest(TurnoAPITestCase):

    def test_api_confirmar_turno_retorna_200(self):
        turno    = self._crear_turno_via_service()
        response = self.client.post(
            self._url_action("confirmar", turno.id),
            {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["estado"], EstadoTurno.CONFIRMADO)

    def test_api_confirmar_turno_con_precio(self):
        turno    = self._crear_turno_via_service()
        response = self.client.post(
            self._url_action("confirmar", turno.id),
            {"precio_final": "2500.00"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["precio_final"], "2500.00")

    def test_api_confirmar_turno_ya_confirmado_retorna_409(self):
        turno = self._crear_turno_via_service()
        TurnoService.confirmar_turno(turno)

        response = self.client.post(
            self._url_action("confirmar", turno.id),
            {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


# ─────────────────────────────────────────────────────────────────────────────
# Cancelar turno
# ─────────────────────────────────────────────────────────────────────────────

class CancelarTurnoAPITest(TurnoAPITestCase):

    def test_api_cancelar_turno_retorna_200(self):
        turno    = self._crear_turno_via_service()
        response = self.client.post(
            self._url_action("cancelar", turno.id),
            {"cancelado_por": ActorCancelacion.CLIENTE}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["estado"], EstadoTurno.CANCELADO)

    def test_api_cancelar_sin_actor_retorna_400(self):
        turno    = self._crear_turno_via_service()
        response = self.client.post(
            self._url_action("cancelar", turno.id),
            {}, format="json",  # cancelado_por required
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_cancelar_actor_invalido_retorna_400(self):
        turno    = self._crear_turno_via_service()
        response = self.client.post(
            self._url_action("cancelar", turno.id),
            {"cancelado_por": "FANTASMA"}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_cancelar_turno_terminal_retorna_409(self):
        turno = self._crear_turno_via_service()
        TurnoService.cancelar_turno(turno, cancelado_por=ActorCancelacion.SISTEMA)

        response = self.client.post(
            self._url_action("cancelar", turno.id),
            {"cancelado_por": ActorCancelacion.SISTEMA}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


# ─────────────────────────────────────────────────────────────────────────────
# Reprogramar turno
# ─────────────────────────────────────────────────────────────────────────────

class ReprogramarTurnoAPITest(TurnoAPITestCase):

    def test_api_reprogramar_turno_retorna_200(self):
        turno    = self._crear_turno_via_service(hora=10)
        response = self.client.post(
            self._url_action("reprogramar", turno.id),
            {"nueva_fecha_inicio": tomorrow_at(14).isoformat()}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_api_reprogramar_actualiza_fecha_inicio(self):
        turno       = self._crear_turno_via_service(hora=10)
        nueva_hora  = tomorrow_at(15)
        response = self.client.post(
            self._url_action("reprogramar", turno.id),
            {"nueva_fecha_inicio": nueva_hora.isoformat()}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The response fecha_inicio must match the new time (same hour)
        import datetime
        fi = datetime.datetime.fromisoformat(response.data["fecha_inicio"])
        self.assertEqual(fi.hour, 15)

    def test_api_reprogramar_a_slot_ocupado_retorna_409(self):
        turno_a = self._crear_turno_via_service(hora=10)
        turno_b = self._crear_turno_via_service(hora=14)

        response = self.client.post(
            self._url_action("reprogramar", turno_a.id),
            {"nueva_fecha_inicio": tomorrow_at(14).isoformat()}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_api_reprogramar_fecha_pasada_retorna_400(self):
        turno = self._crear_turno_via_service(hora=10)
        pasado = timezone.now() - timedelta(hours=3)
        response = self.client.post(
            self._url_action("reprogramar", turno.id),
            {"nueva_fecha_inicio": pasado.isoformat()}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ─────────────────────────────────────────────────────────────────────────────
# Completar / Ausente
# ─────────────────────────────────────────────────────────────────────────────

class CompletarAusenteAPITest(TurnoAPITestCase):

    def _turno_confirmado(self, hora=10):
        turno = self._crear_turno_via_service(hora=hora)
        TurnoService.confirmar_turno(turno)
        turno.refresh_from_db()
        return turno

    def test_api_completar_retorna_200(self):
        turno    = self._turno_confirmado()
        response = self.client.post(
            self._url_action("completar", turno.id), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["estado"], EstadoTurno.COMPLETADO)

    def test_api_ausente_retorna_200(self):
        turno    = self._turno_confirmado(hora=12)
        response = self.client.post(
            self._url_action("ausente", turno.id), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["estado"], EstadoTurno.AUSENTE)

    def test_api_completar_desde_pendiente_retorna_409(self):
        """PENDIENTE → COMPLETADO is invalid — must return 409."""
        turno    = self._crear_turno_via_service(hora=11)
        response = self.client.post(
            self._url_action("completar", turno.id), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)


# ─────────────────────────────────────────────────────────────────────────────
# Slots endpoint
# ─────────────────────────────────────────────────────────────────────────────

class SlotsAPITest(TurnoAPITestCase):

    def _url_slots(self):
        return reverse("turno-slots")

    def test_api_slots_retorna_200(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(self._url_slots(), {
            "fecha":    tomorrow,
            "servicio": str(self.servicio.id),
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_api_slots_respuesta_tiene_count_y_slots(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(self._url_slots(), {
            "fecha":    tomorrow,
            "servicio": str(self.servicio.id),
        })
        self.assertIn("count", response.data)
        self.assertIn("slots", response.data)

    def test_api_slots_sin_fecha_retorna_400(self):
        response = self.client.get(self._url_slots(), {
            "servicio": str(self.servicio.id),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_slots_sin_servicio_retorna_400(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(self._url_slots(), {"fecha": tomorrow})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_slots_fecha_invalida_retorna_400(self):
        response = self.client.get(self._url_slots(), {
            "fecha":    "no-es-una-fecha",
            "servicio": str(self.servicio.id),
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_api_slots_dia_sin_horario_retorna_lista_vacia(self):
        """A date with no HorarioDisponible returns count=0 and empty slots."""
        # Find a day that does NOT have a horario (skip 2 days from tomorrow)
        tomorrow    = date.today() + timedelta(days=1)
        tomorrow_wd = tomorrow.weekday()
        target      = tomorrow + timedelta(days=2)
        if target.weekday() == tomorrow_wd:
            target = tomorrow + timedelta(days=3)

        response = self.client.get(self._url_slots(), {
            "fecha":    target.isoformat(),
            "servicio": str(self.servicio.id),
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["slots"], [])

    def test_api_slots_slot_field_structure(self):
        """Each slot must contain fecha_inicio, fecha_fin, duracion_minutos, profesional."""
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.get(self._url_slots(), {
            "fecha":    tomorrow,
            "servicio": str(self.servicio.id),
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        if response.data["slots"]:
            slot = response.data["slots"][0]
            for field in ("fecha_inicio", "fecha_fin", "duracion_minutos", "profesional"):
                self.assertIn(field, slot, f"Slot missing field: {field}")


# ─────────────────────────────────────────────────────────────────────────────
# Module not active
# ─────────────────────────────────────────────────────────────────────────────

class ModuloInactivoAPITest(APITestCase):
    """Requests when the 'turnos' module is NOT active must return 403."""

    def setUp(self):
        from modules.turnos.tests.factories import make_empresa, make_admin
        self.empresa = make_empresa()
        self.usuario = make_admin(self.empresa)
        # Module intentionally NOT activated
        tokens = get_tokens_for_user(self.usuario)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_listar_sin_modulo_activo_retorna_403(self):
        response = self.client.get(reverse("turno-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
