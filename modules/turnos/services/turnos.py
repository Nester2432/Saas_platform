"""
modules/turnos/services/turnos.py

TurnoService — all write operations for the turnos module.

Contract:
    - Every public method is @transaction.atomic.
    - Every method that creates or reschedules a turno uses select_for_update()
      to prevent double booking under concurrent requests.
    - Business logic lives here. Views call exactly one service method per action.
    - DisponibilidadService is called as a read-only precondition — never the
      reverse (dependency is unidirectional: TurnoService → DisponibilidadService).

Concurrency model — why select_for_update() is necessary:
    Without a DB-level lock, two concurrent POST /turnos/ requests can both:
        T=0ms  A reads: no turno for García 10:00–11:00  → free
        T=1ms  B reads: no turno for García 10:00–11:00  → free
        T=2ms  A inserts turno García 10:00–11:00        → OK
        T=3ms  B inserts turno García 10:00–11:00        → double booking!

    select_for_update() acquires a row-level lock on ALL active turnos for the
    target professional in the target range. Transaction B blocks at T=1ms until
    A commits at T=2ms. B then re-reads, finds A's turno, and raises
    TurnoNoDisponibleError. This serialises concurrent bookings for the same
    professional at the DB transaction level.

    Note: select_for_update() requires an active transaction (@transaction.atomic).
    The lock is held until the transaction commits or rolls back.

State machine transitions (enforced by _validar_transicion):
    PENDIENTE  → CONFIRMADO   (confirmar_turno)
    PENDIENTE  → CANCELADO    (cancelar_turno)
    CONFIRMADO → CANCELADO    (cancelar_turno)
    CONFIRMADO → COMPLETADO   (marcar_completado)
    CONFIRMADO → AUSENTE      (marcar_ausente)
    ── all other transitions raise TransicionInvalidaError ──

Terminal states — no further transitions:
    COMPLETADO, CANCELADO, AUSENTE
"""

import logging
from decimal import Decimal
from datetime import timedelta
from typing import Optional
from uuid import UUID

from django.db import transaction
from django.core.exceptions import ValidationError

from modules.turnos.exceptions import TurnoNoDisponibleError, TransicionInvalidaError
from modules.turnos.models import (
    ActorCancelacion,
    EstadoTurno,
    Profesional,
    ProfesionalServicio,
    Servicio,
    Turno,
)
from modules.turnos.services.disponibilidad import DisponibilidadService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Valid state machine transitions
# ─────────────────────────────────────────────────────────────────────────────

# Maps (estado_actual, estado_destino) → True for allowed transitions.
# Any pair not in this set raises TransicionInvalidaError.
_TRANSICIONES_VALIDAS: set[tuple[str, str]] = {
    (EstadoTurno.PENDIENTE,   EstadoTurno.CONFIRMADO),
    (EstadoTurno.PENDIENTE,   EstadoTurno.CANCELADO),
    (EstadoTurno.CONFIRMADO,  EstadoTurno.CANCELADO),
    (EstadoTurno.CONFIRMADO,  EstadoTurno.COMPLETADO),
    (EstadoTurno.CONFIRMADO,  EstadoTurno.AUSENTE),
}


class TurnoService:
    """
    Mutation service for Turno lifecycle management.

    All methods are static — no instance state, fully thread-safe.
    All public methods are @transaction.atomic.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Public API — mutations
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def crear_turno(
        empresa,
        profesional: Profesional,
        servicio: Servicio,
        fecha_inicio,
        cliente=None,
        notas_cliente: str = "",
        notas_internas: str = "",
        usuario=None,
    ) -> Turno:
        """
        Book a new appointment and return the created Turno.

        Steps (all inside a single DB transaction):
            1. Tenant guard — profesional and servicio belong to empresa.
            2. Capability guard — profesional offers this servicio.
            3. Resolve effective duration (override or service default).
            4. Calculate fecha_fin = fecha_inicio + duration.
            5. Acquire DB lock (select_for_update) on overlapping active turnos.
            6. Verify availability (HorarioDisponible → BloqueoHorario → Turnos).
            7. Create Turno with estado=PENDIENTE.
            8. Log and return.

        Args:
            empresa:          Tenant. All entities must belong to this empresa.
            profesional:      The professional who will perform the service.
            servicio:         The service being booked.
            fecha_inicio:     Appointment start (timezone-aware datetime).
            cliente:          Optional client. None for walk-ins or internal blocks.
            notas_cliente:    Client-provided notes / special requests.
            notas_internas:   Staff-only notes.
            usuario:          Authenticated user performing the action (audit trail).

        Returns:
            Turno with estado=PENDIENTE.

        Raises:
            ValidationError:          Tenant mismatch or profesional doesn't offer
                                      the service.
            TurnoNoDisponibleError:   Slot is unavailable (FUERA_DE_HORARIO,
                                      BLOQUEO_ACTIVO, or TURNO_EXISTENTE).
        """
        # ── Step 1: tenant guards ─────────────────────────────────────────────
        TurnoService._validar_tenant(empresa, profesional=profesional, servicio=servicio)

        # ── Step 2: capability guard ──────────────────────────────────────────
        prof_servicio = TurnoService._get_profesional_servicio(profesional, servicio)

        # ── Step 3: resolve effective duration ────────────────────────────────
        # Use the professional's override if set, otherwise the service default.
        duracion = prof_servicio.duracion_override or servicio.duracion_minutos

        # ── Step 4: calculate fecha_fin ───────────────────────────────────────
        fecha_fin = fecha_inicio + timedelta(minutes=duracion)

        # ── Step 5: DB lock ───────────────────────────────────────────────────
        # Lock all active turnos for this professional that overlap the target
        # range. This blocks any concurrent transaction that tries to book the
        # same slot until this transaction commits or rolls back.
        #
        # Why filter by overlap range before locking?
        # select_for_update() locks exactly the rows returned by the queryset.
        # Filtering to the overlapping range minimises lock contention —
        # bookings for different time slots on the same professional don't block
        # each other. Only truly conflicting requests serialize.
        #
        # Index used: idx_turno_empresa_profesional_estado
        #             + idx_turno_empresa_profesional_fin (for fecha_fin__gt)
        Turno.objects.select_for_update().filter(
            empresa=empresa,
            profesional=profesional,
            estado__in=[EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO],
            fecha_inicio__lt=fecha_fin,
            fecha_fin__gt=fecha_inicio,
        )
        # The queryset is evaluated here (forces the lock) but the result is
        # not needed — we only care about the lock, not which rows matched.
        # The availability check below re-reads through the same transaction.

        # ── Step 6: availability check ────────────────────────────────────────
        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=profesional,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
        )
        if not resultado.disponible:
            raise TurnoNoDisponibleError(
                motivo=resultado.motivo,
                conflicto=resultado.conflicto,
            )

        # ── Step 7: create turno ──────────────────────────────────────────────
        turno = Turno.objects.create(
            empresa=empresa,
            profesional=profesional,
            servicio=servicio,
            cliente=cliente,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            estado=EstadoTurno.PENDIENTE,
            notas_cliente=notas_cliente.strip(),
            notas_internas=notas_internas.strip(),
            precio_final=None,      # set at confirmation time (price snapshot)
            created_by=usuario,
            updated_by=usuario,
        )

        logger.info(
            "Turno created: id=%s empresa=%s profesional=%s servicio=%s "
            "fecha=%s duracion=%dmin",
            turno.id, empresa.id, profesional.id, servicio.id,
            fecha_inicio.isoformat(), duracion,
        )
        return turno

    @staticmethod
    @transaction.atomic
    def confirmar_turno(
        turno: Turno,
        precio_final: Optional[Decimal] = None,
        usuario=None,
    ) -> Turno:
        """
        Confirm a PENDIENTE turno and snapshot the price.

        Transition: PENDIENTE → CONFIRMADO

        precio_final is set here (not at creation) because confirmation is the
        moment the service is "sold" — it's the point in time whose price
        matters for billing and historical accuracy.

        If precio_final is not provided, falls back to servicio.precio.
        If servicio.precio is also None (negotiated pricing), precio_final
        remains None — the caller is responsible for providing it explicitly
        in that case.

        Args:
            turno:        The turno to confirm. Must be in estado=PENDIENTE.
            precio_final: Price to record. None → use servicio.precio.
            usuario:      User performing the action (audit trail).

        Returns:
            Updated Turno with estado=CONFIRMADO.

        Raises:
            TransicionInvalidaError: turno is not in estado=PENDIENTE.
        """
        TurnoService._validar_transicion(turno, EstadoTurno.CONFIRMADO)

        # Snapshot the price. Resolves in priority order:
        #   1. Explicitly provided precio_final argument
        #   2. Service catalog price at this moment in time
        #   3. None (negotiated pricing — no fixed amount)
        precio = precio_final if precio_final is not None else turno.servicio.precio

        turno.estado = EstadoTurno.CONFIRMADO
        turno.precio_final = precio
        turno.updated_by = usuario
        turno.save(update_fields=["estado", "precio_final", "updated_by", "updated_at"])

        logger.info(
            "Turno confirmed: id=%s precio_final=%s usuario=%s",
            turno.id, precio, getattr(usuario, "id", None),
        )
        return turno

    @staticmethod
    @transaction.atomic
    def cancelar_turno(
        turno: Turno,
        cancelado_por: str,
        motivo: str = "",
        usuario=None,
    ) -> Turno:
        """
        Cancel an active turno.

        Transitions:
            PENDIENTE  → CANCELADO
            CONFIRMADO → CANCELADO

        cancelado_por is required (not optional) in the method signature —
        a cancellation without attribution is a data quality problem.
        The DB CheckConstraint enforces the same rule at the storage layer.

        Args:
            turno:        The turno to cancel. Must be PENDIENTE or CONFIRMADO.
            cancelado_por: Who is cancelling. Must be an ActorCancelacion value:
                           PROFESIONAL | CLIENTE | SISTEMA
            motivo:       Optional free-text reason.
            usuario:      User performing the action (audit trail).

        Returns:
            Updated Turno with estado=CANCELADO.

        Raises:
            TransicionInvalidaError: turno is in a terminal state or already
                                     CANCELADO.
            ValidationError:         cancelado_por is not a valid ActorCancelacion.
        """
        TurnoService._validar_transicion(turno, EstadoTurno.CANCELADO)
        TurnoService._validar_actor_cancelacion(cancelado_por)

        turno.estado = EstadoTurno.CANCELADO
        turno.cancelado_por = cancelado_por
        turno.motivo_cancelacion = motivo.strip()
        turno.updated_by = usuario
        turno.save(update_fields=[
            "estado", "cancelado_por", "motivo_cancelacion",
            "updated_by", "updated_at",
        ])

        logger.info(
            "Turno cancelled: id=%s cancelado_por=%s motivo=%r usuario=%s",
            turno.id, cancelado_por, motivo, getattr(usuario, "id", None),
        )
        return turno

    @staticmethod
    @transaction.atomic
    def reprogramar_turno(
        turno: Turno,
        nueva_fecha_inicio,
        usuario=None,
    ) -> Turno:
        """
        Reschedule a turno to a new start time, keeping the same duration.

        Transitions (estado does not change):
            PENDIENTE  → PENDIENTE   (stays the same)
            CONFIRMADO → CONFIRMADO  (stays the same)

        The duration is preserved from the original booking:
            nueva_fecha_fin = nueva_fecha_inicio + original_duration

        This means the service and professional are unchanged — only the time
        slot moves. If the client wants a different service, they must cancel
        and rebook.

        Double-booking prevention uses select_for_update() with
        excluir_turno_id=turno.id so the turno doesn't conflict with its own
        current slot during the availability check (critical — without this,
        rescheduling to any other time would always fail because the current
        slot is still "occupied" by this turno).

        Args:
            turno:             The turno to reschedule. Must be PENDIENTE or CONFIRMADO.
            nueva_fecha_inicio: New start time (timezone-aware datetime).
            usuario:           User performing the action (audit trail).

        Returns:
            Updated Turno with new fecha_inicio / fecha_fin. Estado unchanged.

        Raises:
            TransicionInvalidaError:  turno is in a terminal state.
            TurnoNoDisponibleError:   new slot is unavailable.
        """
        # Validate that the turno is still mutable (not in a terminal state).
        # We reuse _validar_transicion with a synthetic "REPROGRAMADO" target
        # that isn't a real state — or simpler: check terminal directly.
        if turno.es_terminal:
            raise TransicionInvalidaError(
                estado_actual=turno.estado,
                estado_destino="REPROGRAMADO",
            )

        # Preserve the original duration exactly
        duracion = turno.duracion_minutos            # property: (fecha_fin - fecha_inicio)
        nueva_fecha_fin = nueva_fecha_inicio + timedelta(minutes=duracion)

        # ── DB lock ───────────────────────────────────────────────────────────
        # Lock overlapping active turnos for the professional in the NEW range.
        # Exclude the turno being rescheduled so it doesn't lock itself.
        # Index: idx_turno_empresa_profesional_estado
        Turno.objects.select_for_update().filter(
            empresa=turno.empresa,
            profesional=turno.profesional,
            estado__in=[EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO],
            fecha_inicio__lt=nueva_fecha_fin,
            fecha_fin__gt=nueva_fecha_inicio,
        ).exclude(id=turno.id)
        # Force queryset evaluation to acquire the lock immediately.

        # ── Availability check ────────────────────────────────────────────────
        # Pass excluir_turno_id=turno.id so _check_turnos_existentes does not
        # treat the current slot as a conflict with itself.
        resultado = DisponibilidadService.verificar_disponibilidad(
            profesional=turno.profesional,
            fecha_inicio=nueva_fecha_inicio,
            fecha_fin=nueva_fecha_fin,
            excluir_turno_id=turno.id,
        )
        if not resultado.disponible:
            raise TurnoNoDisponibleError(
                motivo=resultado.motivo,
                conflicto=resultado.conflicto,
            )

        fecha_anterior = turno.fecha_inicio     # for logging

        turno.fecha_inicio = nueva_fecha_inicio
        turno.fecha_fin = nueva_fecha_fin
        turno.updated_by = usuario
        turno.save(update_fields=["fecha_inicio", "fecha_fin", "updated_by", "updated_at"])

        logger.info(
            "Turno rescheduled: id=%s %s → %s usuario=%s",
            turno.id,
            fecha_anterior.isoformat(),
            nueva_fecha_inicio.isoformat(),
            getattr(usuario, "id", None),
        )
        return turno

    @staticmethod
    @transaction.atomic
    def marcar_completado(turno: Turno, usuario=None) -> Turno:
        """
        Mark a CONFIRMADO turno as completed.

        Transition: CONFIRMADO → COMPLETADO

        COMPLETADO is a terminal state — no further transitions are allowed.
        Can only be called on CONFIRMADO turnos (not PENDIENTE) because
        completion implies the appointment actually happened, which requires
        prior confirmation.

        Args:
            turno:   The turno to complete. Must be in estado=CONFIRMADO.
            usuario: User performing the action (audit trail).

        Returns:
            Updated Turno with estado=COMPLETADO.

        Raises:
            TransicionInvalidaError: turno is not in estado=CONFIRMADO.
        """
        TurnoService._validar_transicion(turno, EstadoTurno.COMPLETADO)

        turno.estado = EstadoTurno.COMPLETADO
        turno.updated_by = usuario
        turno.save(update_fields=["estado", "updated_by", "updated_at"])

        logger.info(
            "Turno completed: id=%s usuario=%s",
            turno.id, getattr(usuario, "id", None),
        )
        return turno

    @staticmethod
    @transaction.atomic
    def marcar_ausente(turno: Turno, usuario=None) -> Turno:
        """
        Mark a CONFIRMADO turno as no-show (client did not attend).

        Transition: CONFIRMADO → AUSENTE

        AUSENTE is a terminal state — no further transitions are allowed.
        Like COMPLETADO, can only be applied to CONFIRMADO turnos. A PENDIENTE
        turno that the client doesn't attend should be cancelled, not marked
        AUSENTE, because it was never confirmed.

        Args:
            turno:   The turno to mark absent. Must be in estado=CONFIRMADO.
            usuario: User performing the action (audit trail).

        Returns:
            Updated Turno with estado=AUSENTE.

        Raises:
            TransicionInvalidaError: turno is not in estado=CONFIRMADO.
        """
        TurnoService._validar_transicion(turno, EstadoTurno.AUSENTE)

        turno.estado = EstadoTurno.AUSENTE
        turno.updated_by = usuario
        turno.save(update_fields=["estado", "updated_by", "updated_at"])

        logger.info(
            "Turno no-show: id=%s usuario=%s",
            turno.id, getattr(usuario, "id", None),
        )
        return turno

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers — validation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _validar_transicion(turno: Turno, estado_destino: str) -> None:
        """
        Enforce the state machine — raise TransicionInvalidaError if the
        (current state → target state) pair is not in _TRANSICIONES_VALIDAS.

        Called at the top of every mutation method before any DB write.
        This is the single source of truth for all allowed transitions.

        Args:
            turno:           The turno whose estado is being checked.
            estado_destino:  The intended next state.

        Raises:
            TransicionInvalidaError: if the transition is not allowed.
        """
        par = (turno.estado, estado_destino)
        if par not in _TRANSICIONES_VALIDAS:
            raise TransicionInvalidaError(
                estado_actual=turno.estado,
                estado_destino=estado_destino,
            )

    @staticmethod
    def _validar_tenant(empresa, profesional: Profesional, servicio: Servicio) -> None:
        """
        Ensure profesional and servicio both belong to empresa.

        This is the cross-entity tenant guard — models can belong to different
        empresas if someone passes IDs from different tenants (e.g. a crafted
        API request). This check prevents data leakage across tenant boundaries.

        Args:
            empresa:      The request's tenant.
            profesional:  The professional being assigned.
            servicio:     The service being booked.

        Raises:
            ValidationError: if either entity belongs to a different empresa.
        """
        if profesional.empresa_id != empresa.id:
            raise ValidationError(
                "El profesional no pertenece a esta empresa.",
                code="tenant_mismatch",
            )
        if servicio.empresa_id != empresa.id:
            raise ValidationError(
                "El servicio no pertenece a esta empresa.",
                code="tenant_mismatch",
            )

    @staticmethod
    def _get_profesional_servicio(
        profesional: Profesional,
        servicio: Servicio,
    ) -> ProfesionalServicio:
        """
        Return the ProfesionalServicio record linking professional to service.

        This double-checks that the professional actually offers this service —
        a professional may belong to the right empresa but not be configured
        to perform this particular service.

        The returned object provides duracion_override which _may_ differ from
        servicio.duracion_minutos — this is why we go through the join model
        rather than using servicio.duracion_minutos directly.

        Args:
            profesional: The professional being assigned.
            servicio:    The service being booked.

        Returns:
            ProfesionalServicio join record.

        Raises:
            ValidationError: if no active ProfesionalServicio exists for the pair.
        """
        try:
            return ProfesionalServicio.objects.get(
                empresa=profesional.empresa,
                profesional=profesional,
                servicio=servicio,
                deleted_at__isnull=True,
            )
        except ProfesionalServicio.DoesNotExist:
            raise ValidationError(
                f"El profesional '{profesional.nombre_completo}' "
                f"no ofrece el servicio '{servicio.nombre}'.",
                code="servicio_no_ofrecido",
            )

    @staticmethod
    def _validar_actor_cancelacion(cancelado_por: str) -> None:
        """
        Validate that cancelado_por is a valid ActorCancelacion value.

        ActorCancelacion is a TextChoices enum — valid values are:
            PROFESIONAL, CLIENTE, SISTEMA

        Args:
            cancelado_por: The value to validate.

        Raises:
            ValidationError: if the value is not a valid ActorCancelacion.
        """
        valores_validos = ActorCancelacion.values
        if cancelado_por not in valores_validos:
            raise ValidationError(
                f"'{cancelado_por}' no es un actor de cancelación válido. "
                f"Valores permitidos: {valores_validos}",
                code="actor_invalido",
            )
