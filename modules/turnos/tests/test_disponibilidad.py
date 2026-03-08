"""
modules/turnos/tests/test_disponibilidad.py

Tests for DisponibilidadService — the read-only availability layer.

Strategy:
    - Build DB state (horarios, bloqueos, turnos existentes) manually
    - Call service methods directly — no HTTP, no views
    - Assert on the ResultadoDisponibilidad and SlotDisponible return values
    - Never call TurnoService here (service isolation)

All datetimes are timezone-aware (USE_TZ=True).
"""

from datetime import date, time, timedelta

from django.test import TestCase
from django.utils import timezone

from modules.turnos.models import (
    ActorCancelacion,
    BloqueoHorario,
    DiaSemana,
    EstadoTurno,
    Turno,
)
from modules.turnos.services.disponibilidad import (
    DisponibilidadService,
    MOTIVO_FUERA_DE_HORARIO,
    MOTIVO_BLOQUEO_ACTIVO,
    MOTIVO_TURNO_EXISTENTE,
)
from modules.turnos.tests.factories import (
    make_bloqueo,
    make_horario,
    make_profesional,
    make_profesional_servicio,
    make_servicio,
    make_turno,
    setup_turno_completo,
)


def aware(dt):
    """Return dt as timezone-aware if it is naive."""
    if timezone.is_naive(dt):
        return timezone.make_aware(dt)
    return dt


def tomorrow_at(hour: int, minute: int = 0):
    """Return a timezone-aware datetime for tomorrow at the given hour."""
    from datetime import datetime
    tomorrow = date.today() + timedelta(days=1)
    return aware(datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute))


# ─────────────────────────────────────────────────────────────────────────────
# verificar_disponibilidad
# ─────────────────────────────────────────────────────────────────────────────

class VerificarDisponibilidadTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]

    # ── happy path ───────────────────────────────────────────────────────────

    def test_slot_dentro_horario_esta_disponible(self):
        """A slot inside working hours with no conflicts returns disponible=True."""
        inicio = tomorrow_at(10)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertTrue(resultado.disponible)
        self.assertIsNone(resultado.motivo)
        self.assertIsNone(resultado.conflicto)

    # ── FUERA_DE_HORARIO ─────────────────────────────────────────────────────

    def test_slot_fuera_de_horario_no_disponible(self):
        """A slot outside working hours returns FUERA_DE_HORARIO."""
        # HorarioDisponible goes 08:00–20:00. 22:00 is outside.
        inicio = tomorrow_at(22)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertFalse(resultado.disponible)
        self.assertEqual(resultado.motivo, MOTIVO_FUERA_DE_HORARIO)

    def test_profesional_sin_horario_no_disponible(self):
        """A professional with no HorarioDisponible is never available."""
        prof_sin_horario = make_profesional(self.empresa)
        inicio = tomorrow_at(10)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=prof_sin_horario,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertFalse(resultado.disponible)
        self.assertEqual(resultado.motivo, MOTIVO_FUERA_DE_HORARIO)

    def test_slot_cruza_limite_horario_no_disponible(self):
        """A slot that starts inside hours but ends after close is not available."""
        # HorarioDisponible ends at 20:00. Slot 19:30–21:00 crosses the boundary.
        inicio = tomorrow_at(19, 30)
        fin    = tomorrow_at(21, 0)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertFalse(resultado.disponible)
        self.assertEqual(resultado.motivo, MOTIVO_FUERA_DE_HORARIO)

    # ── BLOQUEO_ACTIVO ───────────────────────────────────────────────────────

    def test_slot_durante_bloqueo_no_disponible(self):
        """A slot overlapping a BloqueoHorario returns BLOQUEO_ACTIVO."""
        bloqueo_inicio = tomorrow_at(10)
        bloqueo_fin    = tomorrow_at(12)
        make_bloqueo(
            self.empresa, self.profesional,
            fecha_inicio=bloqueo_inicio,
            fecha_fin=bloqueo_fin,
        )

        inicio = tomorrow_at(11)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertFalse(resultado.disponible)
        self.assertEqual(resultado.motivo, MOTIVO_BLOQUEO_ACTIVO)
        self.assertIsNotNone(resultado.conflicto)

    def test_slot_adyacente_al_bloqueo_disponible(self):
        """A slot that ends exactly when a bloqueo starts is OK (non-overlapping)."""
        bloqueo_inicio = tomorrow_at(12)
        bloqueo_fin    = tomorrow_at(14)
        make_bloqueo(
            self.empresa, self.profesional,
            fecha_inicio=bloqueo_inicio,
            fecha_fin=bloqueo_fin,
        )

        # Slot 11:00–12:00 ends exactly when bloqueo starts — no overlap
        inicio = tomorrow_at(11)
        fin    = tomorrow_at(12)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertTrue(resultado.disponible)

    # ── TURNO_EXISTENTE ──────────────────────────────────────────────────────

    def test_slot_con_turno_existente_no_disponible(self):
        """A slot overlapping an existing active turno returns TURNO_EXISTENTE."""
        turno_inicio = tomorrow_at(10)
        make_turno(
            self.empresa, self.profesional, self.servicio,
            fecha_inicio=turno_inicio,
            estado=EstadoTurno.CONFIRMADO,
        )

        # Try to book inside the existing turno's window
        inicio = tomorrow_at(10, 30)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertFalse(resultado.disponible)
        self.assertEqual(resultado.motivo, MOTIVO_TURNO_EXISTENTE)

    def test_turno_cancelado_no_bloquea_slot(self):
        """A CANCELADO turno does not block the same slot."""
        turno_inicio = tomorrow_at(10)
        make_turno(
            self.empresa, self.profesional, self.servicio,
            fecha_inicio=turno_inicio,
            estado=EstadoTurno.CANCELADO,
            cancelado_por=ActorCancelacion.SISTEMA,
        )

        inicio = tomorrow_at(10)
        fin    = inicio + timedelta(hours=1)

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=inicio,
            fecha_fin=fin,
        )

        self.assertTrue(resultado.disponible)

    def test_excluir_turno_id_evita_conflicto_consigo_mismo(self):
        """When rescheduling, the turno must not conflict with its own current slot."""
        turno = make_turno(
            self.empresa, self.profesional, self.servicio,
            fecha_inicio=tomorrow_at(10),
        )

        # Without exclusion: conflicts with itself
        sin_exclusion = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=tomorrow_at(10),
            fecha_fin=tomorrow_at(11),
        )
        self.assertFalse(sin_exclusion.disponible)

        # With exclusion: the slot is free (it's the same turno)
        con_exclusion = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=tomorrow_at(10),
            fecha_fin=tomorrow_at(11),
            excluir_turno_id=turno.id,
        )
        self.assertTrue(con_exclusion.disponible)

    def test_turno_de_otra_empresa_no_bloquea(self):
        """A turno from empresa B does not block availability for empresa A."""
        from modules.turnos.tests.factories import make_empresa
        empresa_b = make_empresa()
        prof_b    = make_profesional(empresa_b)
        serv_b    = make_servicio(empresa_b)
        # Same time slot, different empresa — should not conflict
        make_turno(empresa_b, prof_b, serv_b, fecha_inicio=tomorrow_at(10))

        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=self.profesional,
            fecha_inicio=tomorrow_at(10),
            fecha_fin=tomorrow_at(11),
        )
        self.assertTrue(resultado.disponible)


# ─────────────────────────────────────────────────────────────────────────────
# obtener_slots_disponibles
# ─────────────────────────────────────────────────────────────────────────────

class ObtenerSlotsDisponiblesTest(TestCase):

    def setUp(self):
        ctx = setup_turno_completo()
        self.empresa     = ctx["empresa"]
        self.profesional = ctx["profesional"]
        self.servicio    = ctx["servicio"]  # duracion_minutos=60
        self.fecha       = date.today() + timedelta(days=1)

    def test_profesional_sin_horario_no_tiene_slots(self):
        """A professional with no HorarioDisponible returns empty slot list."""
        prof = make_profesional(self.empresa)
        make_profesional_servicio(self.empresa, prof, self.servicio)
        # No HorarioDisponible created

        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=prof,
        )

        self.assertEqual(slots, [])

    def test_generacion_slots_basica(self):
        """A professional with an 8h window produces slots at 1h intervals."""
        # HorarioDisponible 08:00–20:00 with 60min service → 12 slots
        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        self.assertGreater(len(slots), 0)
        # First slot starts at 08:00
        self.assertEqual(slots[0].fecha_inicio.hour, 8)
        self.assertEqual(slots[0].fecha_inicio.minute, 0)

    def test_slots_respetan_duracion_servicio(self):
        """Each slot duration equals servicio.duracion_minutos."""
        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        for slot in slots:
            duracion = int((slot.fecha_fin - slot.fecha_inicio).total_seconds() / 60)
            self.assertEqual(duracion, self.servicio.duracion_minutos)

    def test_slots_excluyen_turnos_existentes(self):
        """Slots occupied by existing active turnos are not returned."""
        # Book the 10:00 slot
        make_turno(
            self.empresa, self.profesional, self.servicio,
            fecha_inicio=aware(
                __import__("datetime").datetime(
                    self.fecha.year, self.fecha.month, self.fecha.day, 10, 0
                )
            ),
            estado=EstadoTurno.CONFIRMADO,
        )

        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        slot_starts = [s.fecha_inicio.hour for s in slots]
        self.assertNotIn(10, slot_starts, "Slot at 10:00 should be excluded")

    def test_slots_respetan_dias_laborales(self):
        """Slots are only generated for days matching HorarioDisponible.dia_semana."""
        # Find a day that is NOT in the professional's horario
        # setUp creates horario for tomorrow's weekday only
        tomorrow = date.today() + timedelta(days=1)
        tomorrow_wd = tomorrow.weekday()
        # Pick a different weekday — shift by 2 days (unlikely to match)
        different_day = tomorrow + timedelta(days=2)
        different_wd = different_day.weekday()

        if different_wd == tomorrow_wd:
            different_day = tomorrow + timedelta(days=3)

        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=different_day,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        self.assertEqual(slots, [], "No slots on a day without HorarioDisponible")

    def test_slots_excluyen_bloqueos(self):
        """Slots overlapping a BloqueoHorario are not returned."""
        # Block 10:00–12:00
        bloqueo_inicio = aware(
            __import__("datetime").datetime(
                self.fecha.year, self.fecha.month, self.fecha.day, 10, 0
            )
        )
        make_bloqueo(
            self.empresa, self.profesional,
            fecha_inicio=bloqueo_inicio,
            fecha_fin=bloqueo_inicio + timedelta(hours=2),
        )

        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        for slot in slots:
            # No slot should overlap the blocked window
            self.assertFalse(
                slot.fecha_inicio < bloqueo_inicio + timedelta(hours=2)
                and slot.fecha_fin > bloqueo_inicio,
                f"Slot {slot.fecha_inicio}–{slot.fecha_fin} overlaps bloqueo",
            )

    def test_slots_sin_profesional_retorna_todos_los_profesionales(self):
        """Without a profesional filter, slots from all professionals are returned."""
        # Add a second professional offering the same service
        prof2 = make_profesional(self.empresa)
        make_profesional_servicio(self.empresa, prof2, self.servicio)
        make_horario(
            self.empresa, prof2,
            dia_semana=(date.today() + timedelta(days=1)).weekday(),
            hora_inicio=time(9, 0),
            hora_fin=time(17, 0),
        )

        slots_todos = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=None,  # no filter
        )
        slots_uno = DisponibilidadService.obtener_slots_disponibles(
            empresa=self.empresa,
            fecha=self.fecha,
            servicio=self.servicio,
            profesional=self.profesional,
        )

        self.assertGreater(len(slots_todos), len(slots_uno))

    def test_slots_query_count_es_cuatro(self):
        """obtener_slots_disponibles must use exactly 4 queries (O(1) guarantee)."""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        with CaptureQueriesContext(connection) as ctx:
            DisponibilidadService.obtener_slots_disponibles(
                empresa=self.empresa,
                fecha=self.fecha,
                servicio=self.servicio,
                profesional=self.profesional,
            )

        self.assertEqual(
            len(ctx.captured_queries), 4,
            f"Expected 4 queries, got {len(ctx.captured_queries)}: "
            + str([q["sql"][:60] for q in ctx.captured_queries]),
        )
