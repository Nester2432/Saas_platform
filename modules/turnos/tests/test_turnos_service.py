"""
modules/turnos/tests/test_turnos_service.py

Service-layer tests for TurnoService — including the concurrency test.

Tests call TurnoService methods directly (no HTTP, no views).
Each test class owns a single responsibility.

Concurrency test uses threading to simulate two simultaneous booking
requests for the same slot. select_for_update() serialises them, so
exactly one must succeed and one must raise TurnoNoDisponibleError.
"""

import threading
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from modules.turnos.exceptions import TurnoNoDisponibleError, TransicionInvalidaError
from modules.turnos.models import (
    ActorCancelacion,
    EstadoTurno,
    Turno,
)
from modules.turnos.services import TurnoService
from modules.turnos.tests.factories import (
    make_bloqueo,
    make_profesional,
    make_profesional_servicio,
    make_servicio,
    make_turno,
    setup_turno_completo,
)


def tomorrow_at(hour: int, minute: int = 0):
    from datetime import date, datetime
    from django.utils import timezone as tz
    tomorrow = date.today() + timedelta(days=1)
    dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute)
    return tz.make_aware(dt)


# ─────────────────────────────────────────────────────────────────────────────
# crear_turno
# ─────────────────────────────────────────────────────────────────────────────

class CrearTurnoTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.admin       = ctx["admin"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]

    def test_crear_turno_exitoso(self):
        """Service creates a Turno with estado=PENDIENTE and correct fecha_fin."""
        inicio = tomorrow_at(10)

        turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=inicio,
            usuario=self.admin,
        )

        self.assertIsNotNone(turno.id)
        self.assertEqual(turno.estado, EstadoTurno.PENDIENTE)
        self.assertEqual(turno.empresa, self.empresa)
        self.assertEqual(turno.profesional, self.profesional)
        self.assertEqual(turno.servicio, self.servicio)
        # fecha_fin = fecha_inicio + duracion_minutos
        expected_fin = inicio + timedelta(minutes=self.servicio.duracion_minutos)
        self.assertEqual(turno.fecha_fin, expected_fin)

    def test_crear_turno_persiste_en_db(self):
        """Created turno must be retrievable from the database."""
        turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

        from_db = Turno.objects.get(id=turno.id)
        self.assertEqual(from_db.estado, EstadoTurno.PENDIENTE)

    def test_crear_turno_con_duracion_override(self):
        """If ProfesionalServicio has duracion_override, it overrides servicio.duracion_minutos."""
        # Give this profesional a 30-min override on a 60-min service
        from modules.turnos.models import ProfesionalServicio
        ps = ProfesionalServicio.objects.get(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
        )
        ps.duracion_override = 30
        ps.save(update_fields=["duracion_override"])

        inicio = tomorrow_at(10)
        turno  = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=inicio,
        )

        self.assertEqual(turno.duracion_minutos, 30)
        self.assertEqual(turno.fecha_fin, inicio + timedelta(minutes=30))

    def test_crear_turno_conflicto_horario_lanza_error(self):
        """Creating a turno that overlaps an existing active turno raises TurnoNoDisponibleError."""
        TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

        with self.assertRaises(TurnoNoDisponibleError) as ctx:
            TurnoService.crear_turno(
                empresa=self.empresa,
                profesional=self.profesional,
                servicio=self.servicio,
                fecha_inicio=tomorrow_at(10, 30),  # overlaps first turno
            )

        self.assertEqual(ctx.exception.motivo, "TURNO_EXISTENTE")

    def test_crear_turno_fuera_horario_lanza_error(self):
        """Creating a turno outside working hours raises TurnoNoDisponibleError."""
        with self.assertRaises(TurnoNoDisponibleError) as ctx:
            TurnoService.crear_turno(
                empresa=self.empresa,
                profesional=self.profesional,
                servicio=self.servicio,
                fecha_inicio=tomorrow_at(22),  # HorarioDisponible ends at 20:00
            )

        self.assertEqual(ctx.exception.motivo, "FUERA_DE_HORARIO")

    def test_crear_turno_durante_bloqueo_lanza_error(self):
        """Creating a turno overlapping a BloqueoHorario raises TurnoNoDisponibleError."""
        make_bloqueo(
            self.empresa, self.profesional,
            fecha_inicio=tomorrow_at(10),
            fecha_fin=tomorrow_at(12),
        )

        with self.assertRaises(TurnoNoDisponibleError) as ctx:
            TurnoService.crear_turno(
                empresa=self.empresa,
                profesional=self.profesional,
                servicio=self.servicio,
                fecha_inicio=tomorrow_at(11),
            )

        self.assertEqual(ctx.exception.motivo, "BLOQUEO_ACTIVO")

    def test_crear_turno_profesional_otra_empresa_lanza_error(self):
        """Using a profesional from a different empresa raises ValidationError."""
        from modules.turnos.tests.factories import make_empresa
        otra_empresa = make_empresa()
        prof_otro    = make_profesional(otra_empresa)

        with self.assertRaises(ValidationError):
            TurnoService.crear_turno(
                empresa=self.empresa,
                profesional=prof_otro,
                servicio=self.servicio,
                fecha_inicio=tomorrow_at(10),
            )

    def test_crear_turno_profesional_no_ofrece_servicio_lanza_error(self):
        """Using a profesional who doesn't offer the service raises ValidationError."""
        prof_sin_servicio = make_profesional(self.empresa)
        # No ProfesionalServicio join created

        with self.assertRaises(ValidationError):
            TurnoService.crear_turno(
                empresa=self.empresa,
                profesional=prof_sin_servicio,
                servicio=self.servicio,
                fecha_inicio=tomorrow_at(10),
            )


# ─────────────────────────────────────────────────────────────────────────────
# confirmar_turno
# ─────────────────────────────────────────────────────────────────────────────

class ConfirmarTurnoTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.admin       = ctx["admin"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]
        self.turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

    def test_confirmar_turno_cambia_estado(self):
        """confirmar_turno transitions PENDIENTE → CONFIRMADO."""
        turno = TurnoService.confirmar_turno(self.turno, usuario=self.admin)

        self.assertEqual(turno.estado, EstadoTurno.CONFIRMADO)

    def test_confirmar_turno_persiste_precio_servicio(self):
        """When no precio_final given, servicio.precio is snapshotted."""
        turno = TurnoService.confirmar_turno(self.turno)

        self.assertEqual(str(turno.precio_final), str(self.servicio.precio))

    def test_confirmar_turno_persiste_precio_explicito(self):
        """An explicit precio_final overrides the service catalog price."""
        from decimal import Decimal
        turno = TurnoService.confirmar_turno(self.turno, precio_final=Decimal("999.00"))

        self.assertEqual(turno.precio_final, Decimal("999.00"))

    def test_confirmar_turno_ya_confirmado_lanza_error(self):
        """Confirming an already CONFIRMADO turno raises TransicionInvalidaError."""
        TurnoService.confirmar_turno(self.turno)
        self.turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.confirmar_turno(self.turno)

    def test_confirmar_turno_cancelado_lanza_error(self):
        """Confirming a CANCELADO turno raises TransicionInvalidaError."""
        TurnoService.cancelar_turno(
            self.turno,
            cancelado_por=ActorCancelacion.SISTEMA,
        )
        self.turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.confirmar_turno(self.turno)


# ─────────────────────────────────────────────────────────────────────────────
# cancelar_turno
# ─────────────────────────────────────────────────────────────────────────────

class CancelarTurnoTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]
        self.turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

    def test_cancelar_turno_pendiente(self):
        """PENDIENTE → CANCELADO is a valid transition."""
        turno = TurnoService.cancelar_turno(
            self.turno,
            cancelado_por=ActorCancelacion.CLIENTE,
            motivo="No puedo asistir",
        )

        self.assertEqual(turno.estado, EstadoTurno.CANCELADO)
        self.assertEqual(turno.cancelado_por, ActorCancelacion.CLIENTE)
        self.assertEqual(turno.motivo_cancelacion, "No puedo asistir")

    def test_cancelar_turno_confirmado(self):
        """CONFIRMADO → CANCELADO is a valid transition."""
        TurnoService.confirmar_turno(self.turno)
        self.turno.refresh_from_db()

        turno = TurnoService.cancelar_turno(
            self.turno,
            cancelado_por=ActorCancelacion.PROFESIONAL,
        )

        self.assertEqual(turno.estado, EstadoTurno.CANCELADO)

    def test_cancelar_actor_invalido_lanza_error(self):
        """An invalid cancelado_por value raises ValidationError."""
        with self.assertRaises(ValidationError):
            TurnoService.cancelar_turno(
                self.turno,
                cancelado_por="DESCONOCIDO",
            )

    def test_cancelar_turno_ya_cancelado_lanza_error(self):
        """Cancelling an already CANCELADO turno raises TransicionInvalidaError."""
        TurnoService.cancelar_turno(self.turno, cancelado_por=ActorCancelacion.SISTEMA)
        self.turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.cancelar_turno(self.turno, cancelado_por=ActorCancelacion.SISTEMA)

    def test_cancelar_turno_completado_lanza_error(self):
        """Cancelling a terminal COMPLETADO turno raises TransicionInvalidaError."""
        TurnoService.confirmar_turno(self.turno)
        self.turno.refresh_from_db()
        TurnoService.marcar_completado(self.turno)
        self.turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.cancelar_turno(self.turno, cancelado_por=ActorCancelacion.SISTEMA)


# ─────────────────────────────────────────────────────────────────────────────
# reprogramar_turno
# ─────────────────────────────────────────────────────────────────────────────

class ReprogramarTurnoTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]
        self.turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

    def test_reprogramar_mueve_fecha_inicio(self):
        """Rescheduling updates fecha_inicio and preserves duration."""
        duracion_original = self.turno.duracion_minutos
        nueva_inicio = tomorrow_at(14)

        turno = TurnoService.reprogramar_turno(
            self.turno,
            nueva_fecha_inicio=nueva_inicio,
        )

        self.assertEqual(turno.fecha_inicio, nueva_inicio)
        self.assertEqual(turno.fecha_fin, nueva_inicio + timedelta(minutes=duracion_original))

    def test_reprogramar_preserva_duracion(self):
        """Duration (fecha_fin - fecha_inicio) is identical after rescheduling."""
        original_duracion = self.turno.duracion_minutos
        turno = TurnoService.reprogramar_turno(
            self.turno,
            nueva_fecha_inicio=tomorrow_at(15),
        )

        self.assertEqual(turno.duracion_minutos, original_duracion)

    def test_reprogramar_preserva_estado(self):
        """Estado does not change when rescheduling."""
        self.assertEqual(self.turno.estado, EstadoTurno.PENDIENTE)
        turno = TurnoService.reprogramar_turno(
            self.turno,
            nueva_fecha_inicio=tomorrow_at(14),
        )
        self.assertEqual(turno.estado, EstadoTurno.PENDIENTE)

    def test_reprogramar_a_slot_ocupado_lanza_error(self):
        """Rescheduling to an occupied slot raises TurnoNoDisponibleError."""
        # Create another turno at 14:00
        TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(14),
        )

        with self.assertRaises(TurnoNoDisponibleError):
            TurnoService.reprogramar_turno(
                self.turno,
                nueva_fecha_inicio=tomorrow_at(14),
            )

    def test_reprogramar_turno_terminal_lanza_error(self):
        """Cannot reschedule a terminal turno."""
        TurnoService.cancelar_turno(self.turno, cancelado_por=ActorCancelacion.SISTEMA)
        self.turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.reprogramar_turno(
                self.turno,
                nueva_fecha_inicio=tomorrow_at(14),
            )


# ─────────────────────────────────────────────────────────────────────────────
# marcar_completado / marcar_ausente
# ─────────────────────────────────────────────────────────────────────────────

class EstadosTerminalesTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]

    def _turno_confirmado(self):
        turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(10),
        )
        turno = TurnoService.confirmar_turno(turno)
        turno.refresh_from_db()
        return turno

    def test_marcar_completado_desde_confirmado(self):
        """CONFIRMADO → COMPLETADO is a valid terminal transition."""
        turno = TurnoService.marcar_completado(self._turno_confirmado())
        self.assertEqual(turno.estado, EstadoTurno.COMPLETADO)

    def test_marcar_ausente_desde_confirmado(self):
        """CONFIRMADO → AUSENTE is a valid terminal transition."""
        turno = TurnoService.marcar_ausente(self._turno_confirmado())
        self.assertEqual(turno.estado, EstadoTurno.AUSENTE)

    def test_marcar_completado_desde_pendiente_lanza_error(self):
        """PENDIENTE → COMPLETADO is not a valid transition."""
        turno = TurnoService.crear_turno(
            empresa=self.empresa,
            profesional=self.profesional,
            servicio=self.servicio,
            fecha_inicio=tomorrow_at(11),
        )

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.marcar_completado(turno)

    def test_estado_terminal_no_acepta_mas_transiciones(self):
        """Once in a terminal state, no further transitions are allowed."""
        turno = TurnoService.marcar_completado(self._turno_confirmado())
        turno.refresh_from_db()

        with self.assertRaises(TransicionInvalidaError):
            TurnoService.cancelar_turno(turno, cancelado_por=ActorCancelacion.SISTEMA)


# ─────────────────────────────────────────────────────────────────────────────
# Concurrencia
# ─────────────────────────────────────────────────────────────────────────────

class ConcurrenciaTurnosTest(TransactionTestCase):
    """
    Concurrency tests use TransactionTestCase (not TestCase) because:
    - TestCase wraps every test in a transaction and rolls it back.
    - select_for_update() requires real transaction commits to work.
    - TransactionTestCase issues real COMMITs, flushing the DB between tests.

    Trade-off: TransactionTestCase is slower — use it only where needed.
    """

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]

    def test_dos_creaciones_concurrentes_mismo_slot(self):
        """
        Two concurrent booking requests for the same slot must result in
        exactly ONE turno created. The losing thread must raise TurnoNoDisponibleError.

        Implementation:
            Thread A and Thread B both call TurnoService.crear_turno() for
            the same profesional + fecha_inicio.
            select_for_update() in crear_turno() serialises the two transactions.
            The second thread to acquire the lock sees the turno created by the
            first and raises TurnoNoDisponibleError.

        We collect results via a shared list (thread-safe for append).
        """
        results = []      # (True/False, exception_or_None)
        errors  = []

        def crear():
            try:
                TurnoService.crear_turno(
                    empresa=self.empresa,
                    profesional=self.profesional,
                    servicio=self.servicio,
                    fecha_inicio=tomorrow_at(10),
                )
                results.append(True)
            except TurnoNoDisponibleError:
                results.append(False)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=crear)
        t2 = threading.Thread(target=crear)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No unexpected exceptions
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

        # Exactly one success, one failure
        self.assertEqual(
            results.count(True), 1,
            f"Expected exactly 1 success, got: {results}",
        )
        self.assertEqual(
            results.count(False), 1,
            f"Expected exactly 1 failure, got: {results}",
        )

        # Only one turno exists in the DB
        count = Turno.objects.filter(
            empresa=self.empresa,
            profesional=self.profesional,
        ).count()
        self.assertEqual(count, 1, "Exactly one turno must be persisted")
