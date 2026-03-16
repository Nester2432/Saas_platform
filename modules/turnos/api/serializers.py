"""
modules/turnos/api/serializers.py

Serializers for the turnos module REST API.

Architecture contract:
    - Serializers own INPUT VALIDATION and OUTPUT FORMATTING only.
    - Serializers NEVER execute business logic or call services.
    - Serializers NEVER call .save() for state mutations (TurnoService does that).
    - empresa is NEVER accepted from request body (tenant isolation — injected
      by TenantQuerysetMixin in the view's perform_create()).

Split read / write:
    - TurnoSerializer:            GET responses — rich nested representation.
    - Crear/Confirmar/Cancelar/
      Reprogramar serializers:    POST inputs — flat, explicit, minimal fields.

N+1 contract:
    These serializers ASSUME the view queryset has already called:
        .select_related("profesional", "servicio", "cliente", "created_by")
    Accessing obj.profesional, obj.servicio, obj.cliente inside serializer
    methods will NOT trigger lazy queries when the view honours this contract.
    Accessing obj.profesional_id (the FK column itself) is always safe — it
    reads from the already-loaded row without a join.

Serializer inventory:
    ── Read ───────────────────────────────────────────────────────
    ServicioResumenSerializer       Nested inside TurnoSerializer
    ProfesionalResumenSerializer    Nested inside TurnoSerializer
    ClienteResumenSerializer        Nested inside TurnoSerializer (nullable)
    TurnoSerializer                 Full GET representation
    SlotDisponibleSerializer        GET /turnos/slots/ — booking calendar
    ── Write ──────────────────────────────────────────────────────
    CrearTurnoSerializer            POST /turnos/
    ConfirmarTurnoSerializer        POST /turnos/{id}/confirmar/
    CancelarTurnoSerializer         POST /turnos/{id}/cancelar/
    ReprogramarTurnoSerializer      POST /turnos/{id}/reprogramar/
    MarcarCompletadoSerializer      POST /turnos/{id}/completar/ (no fields)
    MarcarAusenteSerializer         POST /turnos/{id}/ausente/   (no fields)
"""

from django.utils import timezone
from rest_framework import serializers

from modules.turnos.models import (
    ActorCancelacion,
    EstadoTurno,
    Servicio,
    Profesional,
    Turno,
)
from modules.turnos.services.disponibilidad import SlotDisponible


# ─────────────────────────────────────────────────────────────────────────────
# Nested read serializers — used inside TurnoSerializer
# ─────────────────────────────────────────────────────────────────────────────

class ServicioResumenSerializer(serializers.ModelSerializer):
    """
    Compact Servicio representation for embedding inside TurnoSerializer.

    Returns only the fields a client needs to display the appointment card:
    name, duration (to render slot height), price, and calendar color.

    The view's select_related("servicio") ensures this is a JOIN — no
    extra query per turno.
    """

    duracion_display = serializers.CharField(
        read_only=True,
        help_text="Human-readable duration: '45 min' or '1h 30min'."
    )

    class Meta:
        model = Servicio
        fields = [
            "id",
            "nombre",
            "duracion_minutos",
            "duracion_display",
            "precio",
            "color",
        ]
        read_only_fields = fields


class ProfesionalResumenSerializer(serializers.ModelSerializer):
    """
    Compact Profesional representation for embedding inside TurnoSerializer.

    nombre_completo is a model property — safe to call here because
    it only uses name/apellido from the already-loaded row.

    The view's select_related("profesional") ensures no extra query.
    """

    nombre_completo = serializers.CharField(read_only=True)

    class Meta:
        model = Profesional
        fields = [
            "id",
            "nombre",
            "apellido",
            "nombre_completo",
            "color_agenda",
        ]
        read_only_fields = fields


class ClienteResumenSerializer(serializers.Serializer):
    """
    Compact Cliente representation for embedding inside TurnoSerializer.

    Uses a plain Serializer (not ModelSerializer) because Cliente lives in
    a different module (modules.clientes) — importing its model here would
    create a cross-module import. The fields are simple enough to declare
    explicitly without needing ModelSerializer introspection.

    The view's select_related("cliente") ensures no extra query.
    This serializer is used with allow_null=True because cliente is optional
    on a Turno (walk-ins, internal blocks).
    """

    id = serializers.UUIDField(read_only=True)
    nombre = serializers.CharField(read_only=True)
    apellido = serializers.CharField(read_only=True)
    email = serializers.EmailField(read_only=True)
    telefono = serializers.CharField(read_only=True)
    nombre_completo = serializers.CharField(read_only=True)


# ─────────────────────────────────────────────────────────────────────────────
# TurnoSerializer — primary read representation
# ─────────────────────────────────────────────────────────────────────────────

class TurnoSerializer(serializers.ModelSerializer):
    """
    Full read representation of a Turno.

    Used for: GET /turnos/, GET /turnos/{id}/

    Contains all fields needed by a booking calendar UI:
    - Nested profesional, servicio, cliente (prefetched by the view)
    - Human-readable estado_display and cancelado_por_display
    - duracion_minutos computed from fecha_fin - fecha_inicio (model property)
    - Audit fields: created_at, updated_at

    N+1 guarantee:
        All nested serializers (ProfesionalResumen, ServicioResumen, ClienteResumen)
        read from already-loaded related objects. No lazy query is triggered
        as long as the view honours the select_related contract.

    empresa is intentionally excluded — it is an internal tenancy detail, not
    a field API consumers need in the response body.
    """

    profesional = ProfesionalResumenSerializer(read_only=True)
    servicio    = ServicioResumenSerializer(read_only=True)
    cliente     = ClienteResumenSerializer(read_only=True, allow_null=True)

    estado_display = serializers.CharField(
        source="get_estado_display",
        read_only=True,
        help_text="Human-readable estado label.",
    )
    cancelado_por_display = serializers.SerializerMethodField(
        help_text="Human-readable actor label. NULL when not cancelled.",
    )
    duracion_minutos = serializers.SerializerMethodField(
        help_text="Duration in minutes (fecha_fin - fecha_inicio).",
    )

    class Meta:
        model = Turno
        fields = [
            # Identity
            "id",
            # Relationships (nested, read_only)
            "profesional",
            "servicio",
            "cliente",
            # Schedule
            "fecha_inicio",
            "fecha_fin",
            "duracion_minutos",
            # State
            "estado",
            "estado_display",
            # Pricing
            "precio_final",
            # Notes
            "notas_cliente",
            "notas_internas",
            # Cancellation details (null unless estado=CANCELADO)
            "cancelado_por",
            "cancelado_por_display",
            "motivo_cancelacion",
            # Audit
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_cancelado_por_display(self, obj) -> str | None:
        """
        Return the human-readable label for cancelado_por, or None.

        Uses ActorCancelacion.labels dict instead of get_cancelado_por_display()
        because that method is only added by Django when the field uses choices=
        on the model field — which it does, but we resolve it manually here to
        make the logic explicit and testable.
        """
        if not obj.cancelado_por:
            return None
        # ActorCancelacion.choices is a list of (value, label) tuples.
        labels = dict(ActorCancelacion.choices)
        return labels.get(obj.cancelado_por)

    def get_duracion_minutos(self, obj) -> int:
        """
        Return duration in minutes using the model property.

        The property computes (fecha_fin - fecha_inicio).total_seconds() // 60.
        Both fields are on the already-loaded row — no extra query.
        """
        return obj.duracion_minutos


# ─────────────────────────────────────────────────────────────────────────────
# SlotDisponibleSerializer — booking calendar output
# ─────────────────────────────────────────────────────────────────────────────

class SlotDisponibleSerializer(serializers.Serializer):
    """
    Serializes a SlotDisponible dataclass for GET /turnos/slots/.

    SlotDisponible is not a Django model — it's a frozen dataclass returned
    by DisponibilidadService.obtener_slots_disponibles(). We use a plain
    Serializer (not ModelSerializer) because there is no model to introspect.

    The nested profesional uses ProfesionalResumenSerializer because the
    SlotDisponible.profesional attribute is already a fully loaded Profesional
    instance (the service bulk-loaded them all in one query).

    This serializer is READ-ONLY — it never validates write input.
    """

    fecha_inicio     = serializers.DateTimeField(read_only=True)
    fecha_fin        = serializers.DateTimeField(read_only=True)
    duracion_minutos = serializers.IntegerField(read_only=True)
    profesional      = ProfesionalResumenSerializer(read_only=True)


# ─────────────────────────────────────────────────────────────────────────────
# Write serializers — input validation only
# ─────────────────────────────────────────────────────────────────────────────

class CrearTurnoSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/ (book a new appointment).

    Validates the raw request body. Does NOT resolve UUIDs to ORM objects —
    that is the view's responsibility before calling TurnoService.crear_turno().

    Intentionally a plain Serializer (not ModelSerializer) because:
    1. We never call .save() — TurnoService owns the creation.
    2. The input shape (profesional_id, servicio_id) differs from the model
       (profesional FK, servicio FK) — using ModelSerializer would require
       overriding too many defaults.
    3. empresa, fecha_fin, estado are not client-controlled — excluding them
       from ModelSerializer.Meta.fields is error-prone; explicit is better.

    Validations here are STRUCTURAL only (required fields, types, basic
    sanity). Availability, tenant membership, and professional capability
    are all validated inside TurnoService — not here.
    """

    profesional_id = serializers.UUIDField(
        help_text="UUID of the Profesional who will perform the service."
    )
    servicio_id = serializers.UUIDField(
        help_text="UUID of the Servicio being booked."
    )
    fecha_inicio = serializers.DateTimeField(
        help_text="Appointment start time (ISO 8601, timezone-aware)."
    )
    cliente_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
        help_text="UUID of the Cliente. Omit or pass null for walk-ins.",
    )
    notas_cliente = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
        help_text="Client notes or special requests.",
    )
    notas_internas = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
        help_text="Internal staff notes (not visible to the client).",
    )

    def validate_fecha_inicio(self, value):
        """
        Reject appointments in the past.

        This is a structural sanity check, NOT a business rule. It prevents
        obviously invalid requests from reaching the service layer. The
        service still owns the canonical availability check.

        We compare against timezone.now() so the check is always in the
        correct timezone regardless of server clock or client timezone.
        """
        if value <= timezone.now():
            raise serializers.ValidationError(
                "La fecha de inicio debe ser en el futuro."
            )
        return value

    def validate_notas_cliente(self, value):
        return value.strip()

    def validate_notas_internas(self, value):
        return value.strip()


class ConfirmarTurnoSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/{id}/confirmar/.

    precio_final is optional — if omitted, TurnoService falls back to
    servicio.precio (or None for negotiated pricing). The view passes
    validated_data.get("precio_final") to let None propagate correctly.
    """

    precio_final = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
        help_text=(
            "Final price for this appointment. "
            "Omit to use the service's catalog price."
        ),
    )


class CancelarTurnoSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/{id}/cancelar/.

    cancelado_por is required — a cancellation without attribution is a data
    quality problem. ChoiceField validates the value is a valid ActorCancelacion.

    The service's _validar_actor_cancelacion() performs the same check — the
    ChoiceField here provides earlier feedback (serializer validation before
    the view even calls the service) and a better error message format.
    """

    cancelado_por = serializers.ChoiceField(
        choices=ActorCancelacion.choices,
        help_text=(
            "Who is cancelling the appointment. "
            f"Valid values: {', '.join(ActorCancelacion.values)}"
        ),
    )
    motivo = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=1000,
        help_text="Optional reason for cancellation.",
    )

    def validate_motivo(self, value):
        return value.strip()


class ReprogramarTurnoSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/{id}/reprogramar/.

    Only nueva_fecha_inicio is needed — the service preserves the original
    duration and calculates nueva_fecha_fin automatically.

    Same future-date sanity check as CrearTurnoSerializer.validate_fecha_inicio.
    """

    nueva_fecha_inicio = serializers.DateTimeField(
        help_text="New start time for the appointment (ISO 8601, timezone-aware)."
    )

    def validate_nueva_fecha_inicio(self, value):
        """Reject rescheduling to a time in the past."""
        if value <= timezone.now():
            raise serializers.ValidationError(
                "La nueva fecha de inicio debe ser en el futuro."
            )
        return value


class MarcarCompletadoSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/{id}/completar/.

    No fields — the action is self-describing. The turno ID comes from the
    URL, and the acting user from request.user. An empty JSON body {} is
    accepted. The serializer exists to keep the API pattern consistent:
    every action endpoint has a serializer, even if it has no input fields.
    """
    pass


class MarcarAusenteSerializer(serializers.Serializer):
    """
    Input serializer for POST /turnos/{id}/ausente/.

    No fields — same rationale as MarcarCompletadoSerializer.
    """
    pass
