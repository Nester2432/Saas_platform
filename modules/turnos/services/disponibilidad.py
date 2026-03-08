"""
modules/turnos/services/disponibilidad.py

DisponibilidadService — read-only availability queries for the turnos module.

Responsibilities:
    - Answer "can this professional work at this time?" (verificar_disponibilidad)
    - Generate the grid of free slots for a day (obtener_slots_disponibles)

Design contract:
    - NEVER writes to the database.
    - NEVER raises TurnoNoDisponibleError — returns typed result objects instead.
      (TurnoService raises that exception after calling this service.)
    - ALWAYS tenant-scoped: every query starts with empresa filter.
    - Called by: TurnoService (before mutations), views (agenda endpoint),
      background workers (reminder scheduling).

Query count guarantee (O(1) — proven below):
    verificar_disponibilidad:  3 queries (one per check, short-circuits on failure)
    obtener_slots_disponibles: 4 queries regardless of N professionals or M slots
        Q1: profesionales + ProfesionalServicio JOIN
        Q2: HorarioDisponible WHERE profesional_id IN (...) AND dia_semana=?
        Q3: BloqueoHorario   WHERE profesional_id IN (...) AND overlap
        Q4: Turno activos    WHERE profesional_id IN (...) AND overlap
    Python post-processing groups results by profesional_id in memory — no extra
    queries per professional, no matter how many professionals or slots exist.

Index usage (defined in models.py):
    _check_horario_disponible  → idx_horario_empresa_profesional_dia
    _check_bloqueos            → idx_bloqueo_empresa_profesional_inicio/fin
    _check_turnos_existentes   → idx_turno_empresa_profesional_estado
    obtener_slots_disponibles  → all of the above via IN-clause bulk queries
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional
from uuid import UUID

from django.utils import timezone

from modules.turnos.models import (
    BloqueoHorario,
    EstadoTurno,
    HorarioDisponible,
    Profesional,
    ProfesionalServicio,
    Servicio,
    Turno,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────

#: Motivo values returned when disponible=False.
MOTIVO_FUERA_DE_HORARIO = "FUERA_DE_HORARIO"
MOTIVO_BLOQUEO_ACTIVO   = "BLOQUEO_ACTIVO"
MOTIVO_TURNO_EXISTENTE  = "TURNO_EXISTENTE"


@dataclass(frozen=True)
class ResultadoDisponibilidad:
    """
    Result of a single availability check for one professional in one time range.

    disponible=True  → the slot is free, no conflicto
    disponible=False → the slot is blocked; motivo and conflicto explain why

    frozen=True: immutable once created — callers cannot accidentally mutate it.

    Example (available):
        ResultadoDisponibilidad(disponible=True)

    Example (blocked by existing appointment):
        ResultadoDisponibilidad(
            disponible=False,
            motivo=MOTIVO_TURNO_EXISTENTE,
            conflicto=<Turno: ...>,
        )
    """
    disponible: bool
    motivo: Optional[str] = None          # None when disponible=True
    conflicto: Optional[object] = None    # Turno | BloqueoHorario | None


@dataclass(frozen=True)
class SlotDisponible:
    """
    A free appointment slot for a specific professional.

    Returned by obtener_slots_disponibles as part of the booking UI flow.
    The frontend renders these as clickable time blocks on the calendar.

    duracion_minutos is included so the UI can render slot height correctly
    without needing to know the service duration separately.
    """
    fecha_inicio: datetime
    fecha_fin: datetime
    profesional: Profesional
    duracion_minutos: int

    @property
    def hora_inicio(self) -> time:
        return self.fecha_inicio.time()

    @property
    def hora_fin(self) -> time:
        return self.fecha_fin.time()


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────

class DisponibilidadService:
    """
    Read-only service for availability queries.

    All methods are static — no instance state, no side effects.
    Thread-safe by design (no mutable shared state).
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def verificar_disponibilidad(
        profesional: Profesional,
        fecha_inicio: datetime,
        fecha_fin: datetime,
        excluir_turno_id: Optional[UUID] = None,
    ) -> ResultadoDisponibilidad:
        """
        Check whether a professional is available for [fecha_inicio, fecha_fin).

        Runs three checks in order, short-circuiting on the first failure:
            1. HorarioDisponible — does the professional work on that day/time?
            2. BloqueoHorario    — is the professional blocked (holiday, etc.)?
            3. Turno activos     — is there an existing active appointment?

        The order matters for performance: checking HorarioDisponible first
        (pure filter, no overlap math) avoids the more expensive overlap
        queries when the basic schedule check already fails.

        Args:
            profesional:      The professional to check.
            fecha_inicio:     Start of the requested range (inclusive).
            fecha_fin:        End of the requested range (exclusive).
            excluir_turno_id: UUID of a Turno to ignore in the overlap check.
                              Pass the current turno's id when reprogramming
                              so the turno doesn't conflict with itself.

        Returns:
            ResultadoDisponibilidad — never raises, always returns a typed result.

        Query count: 1–3 (short-circuits; worst case runs all three).
        """
        # ── Check 1: recurring schedule ──────────────────────────────────────
        if not DisponibilidadService._check_horario_disponible(
            profesional, fecha_inicio, fecha_fin
        ):
            logger.debug(
                "Disponibilidad: FUERA_DE_HORARIO profesional=%s %s–%s",
                profesional.id, fecha_inicio, fecha_fin,
            )
            return ResultadoDisponibilidad(
                disponible=False,
                motivo=MOTIVO_FUERA_DE_HORARIO,
            )

        # ── Check 2: one-off blocks ───────────────────────────────────────────
        bloqueo = DisponibilidadService._check_bloqueos(
            profesional, fecha_inicio, fecha_fin
        )
        if bloqueo is not None:
            logger.debug(
                "Disponibilidad: BLOQUEO_ACTIVO profesional=%s bloqueo=%s",
                profesional.id, bloqueo.id,
            )
            return ResultadoDisponibilidad(
                disponible=False,
                motivo=MOTIVO_BLOQUEO_ACTIVO,
                conflicto=bloqueo,
            )

        # ── Check 3: existing active appointments ────────────────────────────
        turno_conflicto = DisponibilidadService._check_turnos_existentes(
            profesional, fecha_inicio, fecha_fin, excluir_turno_id
        )
        if turno_conflicto is not None:
            logger.debug(
                "Disponibilidad: TURNO_EXISTENTE profesional=%s turno=%s",
                profesional.id, turno_conflicto.id,
            )
            return ResultadoDisponibilidad(
                disponible=False,
                motivo=MOTIVO_TURNO_EXISTENTE,
                conflicto=turno_conflicto,
            )

        return ResultadoDisponibilidad(disponible=True)

    @staticmethod
    def obtener_slots_disponibles(
        empresa,
        fecha: date,
        servicio: Servicio,
        profesional: Optional[Profesional] = None,
    ) -> list[SlotDisponible]:
        """
        Return all free appointment slots for a given day and service.

        If profesional is provided, returns slots only for that professional.
        If profesional is None, returns slots for ALL active professionals
        who offer the service (multi-staff booking flow).

        Query count: always 4, regardless of number of professionals or slots.
        See module docstring for the full query plan.

        Args:
            empresa:      Tenant scope.
            fecha:        Calendar date to check (date, not datetime).
            servicio:     The service being booked (determines slot duration).
            profesional:  Optional filter — None means all eligible professionals.

        Returns:
            List of SlotDisponible, ordered by (profesional, fecha_inicio).
            Empty list if no slots are available.
        """
        # ── Q1: Resolve professionals ─────────────────────────────────────────
        # Fetch professionals + their ProfesionalServicio row in one query.
        # select_related("empresa") is already on the profesional.
        # We need the ProfesionalServicio row to resolve duracion_override.
        prof_servicios = (
            ProfesionalServicio.objects
            .filter(
                empresa=empresa,
                servicio=servicio,
                deleted_at__isnull=True,
                profesional__activo=True,
                profesional__deleted_at__isnull=True,
            )
            .select_related("profesional", "servicio")
        )
        if profesional is not None:
            prof_servicios = prof_servicios.filter(profesional=profesional)

        prof_servicios = list(prof_servicios)

        if not prof_servicios:
            return []

        # Build lookup structures from Q1 results — no extra queries.
        profesional_ids = [ps.profesional_id for ps in prof_servicios]
        duracion_por_profesional: dict[UUID, int] = {
            ps.profesional_id: ps.duracion_override or servicio.duracion_minutos
            for ps in prof_servicios
        }
        profesional_por_id: dict[UUID, Profesional] = {
            ps.profesional_id: ps.profesional
            for ps in prof_servicios
        }

        # ── Day boundaries (timezone-aware) ───────────────────────────────────
        dia_semana = fecha.weekday()            # 0=Mon … 6=Sun — matches DiaSemana
        dia_inicio = datetime.combine(fecha, time.min)
        dia_fin    = datetime.combine(fecha, time.max)

        # Make timezone-aware if USE_TZ=True
        tz = timezone.get_current_timezone()
        dia_inicio = timezone.make_aware(dia_inicio, tz)
        dia_fin    = timezone.make_aware(dia_fin, tz)

        # ── Q2: All relevant HorarioDisponible rows ───────────────────────────
        # Filter by profesional_id IN (...) AND dia_semana — single query.
        # Index: idx_horario_empresa_profesional_dia
        horarios_qs = HorarioDisponible.objects.filter(
            empresa=empresa,
            profesional_id__in=profesional_ids,
            dia_semana=dia_semana,
            activo=True,
            deleted_at__isnull=True,
        )

        # Group by profesional_id in Python — no further queries.
        horarios_por_profesional: dict[UUID, list[HorarioDisponible]] = {}
        for h in horarios_qs:
            horarios_por_profesional.setdefault(h.profesional_id, []).append(h)

        # ── Q3: All BloqueoHorario overlapping this day ───────────────────────
        # Overlap condition: bloqueo.fecha_inicio < dia_fin AND bloqueo.fecha_fin > dia_inicio
        # Index: idx_bloqueo_empresa_profesional_inicio + fin
        bloqueos_qs = BloqueoHorario.objects.filter(
            empresa=empresa,
            profesional_id__in=profesional_ids,
            fecha_inicio__lt=dia_fin,
            fecha_fin__gt=dia_inicio,
            deleted_at__isnull=True,
        )

        bloqueos_por_profesional: dict[UUID, list[BloqueoHorario]] = {}
        for b in bloqueos_qs:
            bloqueos_por_profesional.setdefault(b.profesional_id, []).append(b)

        # ── Q4: All active Turnos overlapping this day ────────────────────────
        # Same overlap condition, restricted to non-terminal estados.
        # Index: idx_turno_empresa_profesional_estado
        turnos_qs = Turno.objects.filter(
            empresa=empresa,
            profesional_id__in=profesional_ids,
            estado__in=[EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO],
            fecha_inicio__lt=dia_fin,
            fecha_fin__gt=dia_inicio,
            deleted_at__isnull=True,
        )

        turnos_por_profesional: dict[UUID, list[Turno]] = {}
        for t in turnos_qs:
            turnos_por_profesional.setdefault(t.profesional_id, []).append(t)

        # ── Python: generate slots per professional ───────────────────────────
        # Everything from here is pure in-memory computation — zero extra queries.
        slots: list[SlotDisponible] = []

        for ps in prof_servicios:
            prof_id       = ps.profesional_id
            prof_obj      = profesional_por_id[prof_id]
            duracion      = duracion_por_profesional[prof_id]
            horarios      = horarios_por_profesional.get(prof_id, [])
            bloqueos      = bloqueos_por_profesional.get(prof_id, [])
            turnos_activos = turnos_por_profesional.get(prof_id, [])

            prof_slots = DisponibilidadService._generar_slots_profesional(
                profesional=prof_obj,
                fecha=fecha,
                duracion_minutos=duracion,
                horarios=horarios,
                bloqueos=bloqueos,
                turnos_activos=turnos_activos,
                tz=tz,
            )
            slots.extend(prof_slots)

        # Sort: by profesional name first, then by time — deterministic output
        slots.sort(key=lambda s: (s.profesional.nombre_completo, s.fecha_inicio))

        logger.debug(
            "obtener_slots_disponibles: empresa=%s fecha=%s servicio=%s → %d slots",
            empresa.id, fecha, servicio.id, len(slots),
        )
        return slots

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers — single-check queries
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_horario_disponible(
        profesional: Profesional,
        fecha_inicio: datetime,
        fecha_fin: datetime,
    ) -> bool:
        """
        Return True if an active HorarioDisponible covers [fecha_inicio, fecha_fin).

        A "cover" means the horario's [hora_inicio, hora_fin] contains the
        entire requested range (not just overlaps). A 30-min appointment starting
        at 17:45 in a schedule ending at 18:00 is valid; at 17:50 it is not.

        Query: 1 (EXISTS — stops at first matching row)
        Index: idx_horario_empresa_profesional_dia
        """
        return HorarioDisponible.objects.filter(
            empresa=profesional.empresa,
            profesional=profesional,
            dia_semana=fecha_inicio.weekday(),   # 0=Mon … 6=Sun matches DiaSemana
            hora_inicio__lte=fecha_inicio.time(),
            hora_fin__gte=fecha_fin.time(),
            activo=True,
            deleted_at__isnull=True,
        ).exists()

    @staticmethod
    def _check_bloqueos(
        profesional: Profesional,
        fecha_inicio: datetime,
        fecha_fin: datetime,
    ) -> Optional[BloqueoHorario]:
        """
        Return the first BloqueoHorario overlapping [fecha_inicio, fecha_fin),
        or None if the professional is not blocked during that period.

        Overlap condition (Allen's interval algebra):
            bloqueo.fecha_inicio < fecha_fin
            AND bloqueo.fecha_fin  > fecha_inicio

        This catches all overlap types:
            ── partial overlap (bloqueo starts before, ends during)
            ── total containment (bloqueo fully contains the requested range)
            ── reverse containment (requested range fully contains the bloqueo)
            ── partial overlap (bloqueo starts during, ends after)

        Query: 1 (.first() — stops at the first overlapping row)
        Index: idx_bloqueo_empresa_profesional_inicio
               idx_bloqueo_empresa_profesional_fin
        """
        return BloqueoHorario.objects.filter(
            empresa=profesional.empresa,
            profesional=profesional,
            fecha_inicio__lt=fecha_fin,
            fecha_fin__gt=fecha_inicio,
            deleted_at__isnull=True,
        ).first()

    @staticmethod
    def _check_turnos_existentes(
        profesional: Profesional,
        fecha_inicio: datetime,
        fecha_fin: datetime,
        excluir_turno_id: Optional[UUID] = None,
    ) -> Optional[Turno]:
        """
        Return the first active Turno overlapping [fecha_inicio, fecha_fin),
        or None if the slot is free.

        "Active" means estado IN (PENDIENTE, CONFIRMADO).
        Terminal states (COMPLETADO, CANCELADO, AUSENTE) do not block new bookings.

        excluir_turno_id: pass the id of the turno being reprogrammed so it
        does not conflict with itself during the rescheduling availability check.

        Same Allen overlap condition as _check_bloqueos.

        Query: 1 (.first() — stops at the first overlapping row)
        Index: idx_turno_empresa_profesional_estado
               idx_turno_empresa_profesional_fin
        """
        qs = Turno.objects.filter(
            empresa=profesional.empresa,
            profesional=profesional,
            estado__in=[EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO],
            fecha_inicio__lt=fecha_fin,
            fecha_fin__gt=fecha_inicio,
            deleted_at__isnull=True,
        )
        if excluir_turno_id is not None:
            qs = qs.exclude(id=excluir_turno_id)
        return qs.first()

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers — slot generation (pure Python, zero DB queries)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _generar_slots_profesional(
        profesional: Profesional,
        fecha: date,
        duracion_minutos: int,
        horarios: list[HorarioDisponible],
        bloqueos: list[BloqueoHorario],
        turnos_activos: list[Turno],
        tz,
    ) -> list[SlotDisponible]:
        """
        Generate all free slots for one professional on one day.

        This is pure Python — all data is already loaded.
        No database access occurs here.

        Algorithm:
            For each HorarioDisponible franja (working block):
                Generate candidate slots at duracion_minutos intervals
                starting at franja.hora_inicio, ending when the next
                slot would exceed franja.hora_fin.
            Filter out slots that overlap with any BloqueoHorario or Turno.

        Split-shift example:
            horarios = [Mon 09:00-13:00, Mon 15:00-19:00]
            duracion = 60 min
            → candidates = [09:00, 10:00, 11:00, 12:00, 15:00, 16:00, 17:00, 18:00]
            (13:00 excluded because 13:00+60=14:00 > 13:00 franja end)

        Args:
            profesional:    The professional instance (attached to each SlotDisponible).
            fecha:          The calendar date being generated.
            duracion_minutos: Slot duration — already resolved (override or default).
            horarios:       HorarioDisponible rows for this professional on this weekday.
            bloqueos:       BloqueoHorario rows overlapping this day (pre-filtered).
            turnos_activos: Active Turno rows overlapping this day (pre-filtered).
            tz:             Django current timezone for aware datetimes.

        Returns:
            List of SlotDisponible — may be empty.
        """
        slots: list[SlotDisponible] = []
        delta = timedelta(minutes=duracion_minutos)

        for horario in horarios:
            # Combine the date with the schedule's time bounds
            franja_inicio = timezone.make_aware(
                datetime.combine(fecha, horario.hora_inicio), tz
            )
            franja_fin = timezone.make_aware(
                datetime.combine(fecha, horario.hora_fin), tz
            )

            # Walk the franja in duracion_minutos steps
            cursor = franja_inicio
            while cursor + delta <= franja_fin:
                slot_inicio = cursor
                slot_fin    = cursor + delta

                if not DisponibilidadService._slot_esta_ocupado(
                    slot_inicio, slot_fin, bloqueos, turnos_activos
                ):
                    slots.append(
                        SlotDisponible(
                            fecha_inicio=slot_inicio,
                            fecha_fin=slot_fin,
                            profesional=profesional,
                            duracion_minutos=duracion_minutos,
                        )
                    )

                cursor += delta

        return slots

    @staticmethod
    def _slot_esta_ocupado(
        slot_inicio: datetime,
        slot_fin: datetime,
        bloqueos: list[BloqueoHorario],
        turnos_activos: list[Turno],
    ) -> bool:
        """
        Return True if the candidate slot overlaps with any bloqueo or active turno.

        Same Allen overlap predicate used in the DB queries:
            other.inicio < slot_fin AND other.fin > slot_inicio

        Both lists are already filtered for this professional and this day
        (loaded once in obtener_slots_disponibles). This is pure iteration
        over Python lists — O(B + T) where B=bloqueos, T=turnos for that day,
        both typically small numbers (< 20).

        Called once per candidate slot — the outer loop is bounded by
        (working_hours / duracion_minutos), typically 8–16 iterations per
        franja per professional.
        """
        # Check bloqueos
        for bloqueo in bloqueos:
            if bloqueo.fecha_inicio < slot_fin and bloqueo.fecha_fin > slot_inicio:
                return True

        # Check existing turnos
        for turno in turnos_activos:
            if turno.fecha_inicio < slot_fin and turno.fecha_fin > slot_inicio:
                return True

        return False
