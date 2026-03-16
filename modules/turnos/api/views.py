"""
modules/turnos/api/views.py

REST API for the turnos (appointment scheduling) module.

Architecture contract:
    Views are THIN orchestrators — they:
        1. Authenticate + authorise (via permission_classes)
        2. Validate input (via serializer.is_valid())
        3. Resolve ORM objects from validated IDs
        4. Call exactly ONE service method
        5. Serialise and return the response

    Views NEVER:
        - Touch models directly (no .save(), no .create(), no state changes)
        - Execute availability or state-machine logic
        - Call DisponibilidadService (that is TurnoService's job)
        - Access request.data after serializer validation

Query budget (O(1) per endpoint):
    LIST   → 6 queries: empresa(1) + modulo(1) + COUNT(1) + SELECT(1)
                        + select_related profesional+servicio+cliente(0 extra, JOINs)
    DETAIL → 5 queries: empresa(1) + modulo(1) + SELECT+JOINs(1)
    ACTIONS→ 5 queries: empresa(1) + modulo(1) + SELECT+JOINs(1) + service writes

Exception handling:
    Django's ValidationError (from services) is NOT a DRF exception — it does
    not flow through DRF's exception handler automatically. The view must catch
    it and convert it to a DRF ValidationError or a structured Response.
    _handle_service_error() centralises this conversion for all action methods.

Endpoints exposed:
    GET    /turnos/                         → list with filters
    POST   /turnos/                         → crear_turno
    GET    /turnos/{id}/                    → retrieve
    POST   /turnos/{id}/confirmar/          → confirmar_turno
    POST   /turnos/{id}/cancelar/           → cancelar_turno
    POST   /turnos/{id}/reprogramar/        → reprogramar_turno
    POST   /turnos/{id}/completar/          → marcar_completado
    POST   /turnos/{id}/ausente/            → marcar_ausente
    GET    /turnos/slots/                   → obtener_slots_disponibles
"""

import logging
from datetime import date

from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404

from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from core.mixins import TenantQuerysetMixin
from core.pagination import DefaultPagination
from core.permissions.base import IsTenantAuthenticated, ModuloActivoPermission

from modules.turnos.api.permissions import (
    PuedeVerTurnos,
    PuedeCrearTurnos,
    PuedeConfirmarTurnos,
    PuedeCancelarTurnos,
    PuedeReprogramarTurnos,
    PuedeCompletarTurnos,
    TurnoObjectPermission,
)

from modules.turnos.exceptions import TurnoNoDisponibleError, TransicionInvalidaError
from modules.turnos.models import (
    Profesional,
    Servicio,
    Turno,
)
from modules.turnos.api.serializers import (
    TurnoSerializer,
    SlotDisponibleSerializer,
    CrearTurnoSerializer,
    ConfirmarTurnoSerializer,
    CancelarTurnoSerializer,
    ReprogramarTurnoSerializer,
    MarcarCompletadoSerializer,
    MarcarAusenteSerializer,
)
from modules.turnos.services import DisponibilidadService, TurnoService

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Queryset — shared across list, retrieve, and all actions
# ─────────────────────────────────────────────────────────────────────────────

def _turno_queryset(empresa):
    """
    Base queryset for a tenant, with all FKs joined in one SQL query.

    select_related joins profesional, servicio, cliente, and created_by
    via LEFT OUTER JOINs on the same SELECT — zero extra queries per row.

    This is called by get_queryset() (for list/retrieve) and by
    _get_turno() (for action methods) so both paths get the same joins.
    """
    return (
        Turno.objects
        .for_empresa(empresa)
        .select_related(
            "profesional",   # → ProfesionalResumenSerializer
            "servicio",      # → ServicioResumenSerializer
            "cliente",       # → ClienteResumenSerializer (nullable)
            "created_by",    # → audit field in TurnoSerializer
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Exception conversion helper
# ─────────────────────────────────────────────────────────────────────────────

def _handle_service_error(exc: Exception) -> Response:
    """
    Convert domain exceptions from TurnoService into structured HTTP responses.

    Django's ValidationError is not a DRF exception — DRF's exception handler
    does not intercept it automatically. We convert it here so the response
    shape matches the platform's error envelope (from core/exceptions.py).

    Maps:
        TurnoNoDisponibleError  → 409 Conflict  (slot already taken)
        TransicionInvalidaError → 409 Conflict  (invalid state transition)
        DjangoValidationError   → 400 Bad Request (tenant mismatch, etc.)

    409 is correct for both domain errors: the request was syntactically valid
    but conflicts with the current state of the resource (taken slot, wrong state).
    """
    if isinstance(exc, (TurnoNoDisponibleError, TransicionInvalidaError)):
        detail = {
            "error": True,
            "code": exc.code if hasattr(exc, "code") else "conflict",
            "message": str(exc.message) if hasattr(exc, "message") else str(exc),
            "details": {},
        }
        # TurnoNoDisponibleError carries the specific motivo
        if isinstance(exc, TurnoNoDisponibleError):
            detail["details"]["motivo"] = exc.motivo
        # TransicionInvalidaError carries current/intended states
        if isinstance(exc, TransicionInvalidaError):
            detail["details"]["estado_actual"]   = exc.estado_actual
            detail["details"]["estado_destino"]  = exc.estado_destino
        return Response(detail, status=status.HTTP_409_CONFLICT)

    if isinstance(exc, DjangoValidationError):
        # Normalise Django ValidationError messages to a list
        messages = exc.messages if hasattr(exc, "messages") else [str(exc)]
        detail = {
            "error": True,
            "code": getattr(exc, "code", "validation_error") or "validation_error",
            "message": messages[0] if messages else "Error de validación.",
            "details": {"non_field_errors": messages},
        }
        return Response(detail, status=status.HTTP_400_BAD_REQUEST)

    # Unexpected — re-raise so DRF's handler logs it as a 500
    raise exc


# ─────────────────────────────────────────────────────────────────────────────
# TurnoViewSet
# ─────────────────────────────────────────────────────────────────────────────

class TurnoViewSet(TenantQuerysetMixin, viewsets.GenericViewSet):
    """
    ViewSet for appointment lifecycle management.

    Inherits GenericViewSet (not ModelViewSet) because:
    - create goes through TurnoService, not serializer.save()
    - update/partial_update are not exposed (use specific action endpoints)
    - destroy is not exposed (use cancelar instead)

    list and retrieve are added explicitly via mixins imported below,
    giving us full control over the queryset and serializer without
    inheriting ModelViewSet's default update/destroy behaviour.

    Filter params (all optional, combinable):
        ?profesional=<uuid>         filter by profesional ID
        ?cliente=<uuid>             filter by cliente ID
        ?fecha_inicio=<date>        filter turnos starting on or after this date
        ?fecha_fin=<date>           filter turnos starting on or before this date
        ?estado=<EstadoTurno>       filter by estado (PENDIENTE, CONFIRMADO, …)
        ?search=<str>               search in servicio.nombre, profesional fields
        ?ordering=<field>           sort by fecha_inicio, created_at (default: fecha_inicio)
    """

    permission_classes  = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido    = "turnos"
    pagination_class    = DefaultPagination
    serializer_class    = TurnoSerializer
    filter_backends     = [filters.SearchFilter, filters.OrderingFilter]
    search_fields       = ["^profesional__nombre", "^profesional__apellido", "^servicio__nombre"]
    ordering_fields     = ["fecha_inicio", "created_at", "estado"]
    ordering            = ["fecha_inicio"]   # default: chronological

    # ── per-action permission routing ─────────────────────────────────────────

    def get_permissions(self):
        """
        Return the permission list for the current action.

        Layer 1 (always applied): IsTenantAuthenticated + ModuloActivoPermission
            → from permission_classes above — user is authenticated, belongs to
              an empresa, and the empresa has the "turnos" module active.

        Layer 2 (per-action): action-specific permission class
            → answers "can this role perform this action at all?"
            → combined with Layer 1 via DRF's AND semantics (all must pass).

        Layer 3 (object-level): TurnoObjectPermission
            → applied by ViewSet.get_object() on detail routes and actions.
            → answers "can this user access THIS specific turno?"
            → enforces profesional self-scope and defense-in-depth tenant guard.

        Mapping:
            list / retrieve    → PuedeVerTurnos
            create             → PuedeCrearTurnos
            confirmar          → PuedeConfirmarTurnos  + TurnoObjectPermission
            cancelar           → PuedeCancelarTurnos   + TurnoObjectPermission
            reprogramar        → PuedeReprogramarTurnos + TurnoObjectPermission
            completar          → PuedeCompletarTurnos  + TurnoObjectPermission
            ausente            → PuedeCompletarTurnos  + TurnoObjectPermission
            slots              → PuedeCrearTurnos (booking intent required)
        """
        # Base guards always run first — DRF evaluates permission_classes AND
        # the list returned here. To avoid double-evaluation, we return instances
        # explicitly and override permission_classes completely per action.
        base = [IsTenantAuthenticated(), ModuloActivoPermission()]

        action_permissions = {
            "list":        [PuedeVerTurnos()],
            "retrieve":    [PuedeVerTurnos(),        TurnoObjectPermission()],
            "create":      [PuedeCrearTurnos()],
            "confirmar":   [PuedeConfirmarTurnos(),  TurnoObjectPermission()],
            "cancelar":    [PuedeCancelarTurnos(),   TurnoObjectPermission()],
            "reprogramar": [PuedeReprogramarTurnos(), TurnoObjectPermission()],
            "completar":   [PuedeCompletarTurnos(),  TurnoObjectPermission()],
            "ausente":     [PuedeCompletarTurnos(),  TurnoObjectPermission()],
            "slots":       [PuedeCrearTurnos()],
        }

        return base + action_permissions.get(self.action, [PuedeVerTurnos()])

    # ── queryset ─────────────────────────────────────────────────────────────

    def get_queryset(self):
        """
        Tenant-scoped queryset with optional query-param filters.

        TenantQuerysetMixin.get_queryset() calls super().get_queryset() and
        applies .for_empresa() — so all filters below operate on an already-
        tenant-scoped queryset. No empresa filter needed here.

        Filtering strategy:
            All filters use indexed columns (see models.py index list).
            Date filters use fecha_inicio (covered by idx_turno_empresa_inicio).
            FK filters use _id columns directly (no JOIN needed for filtering).
        """
        qs = _turno_queryset(self.request.empresa)

        params = self.request.query_params

        # Filter by profesional (UUID)
        profesional_id = params.get("profesional")
        if profesional_id:
            qs = qs.filter(profesional_id=profesional_id)

        # Filter by cliente (UUID)
        cliente_id = params.get("cliente")
        if cliente_id:
            qs = qs.filter(cliente_id=cliente_id)

        # Filter by estado
        estado = params.get("estado")
        if estado:
            qs = qs.filter(estado=estado)

        # Date range filters — compare against fecha_inicio (the booking date)
        # Accept ISO date strings: "2024-01-15"
        fecha_desde = params.get("fecha_inicio")
        if fecha_desde:
            qs = qs.filter(fecha_inicio__date__gte=fecha_desde)

        fecha_hasta = params.get("fecha_fin")
        if fecha_hasta:
            qs = qs.filter(fecha_inicio__date__lte=fecha_hasta)

        return qs

    # ── list & retrieve (explicit, not from ModelViewSet) ────────────────────

    def list(self, request, *args, **kwargs):
        """GET /turnos/ — paginated list with optional filters."""
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = TurnoSerializer(page, many=True, context={"request": request})
            return self.get_paginated_response(serializer.data)
        serializer = TurnoSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        """GET /turnos/{id}/ — single turno with all nested data."""
        turno = self._get_turno(kwargs["pk"])
        serializer = TurnoSerializer(turno, context={"request": request})
        return Response(serializer.data)

    # ── create ───────────────────────────────────────────────────────────────

    def create(self, request, *args, **kwargs):
        """
        POST /turnos/

        Validates input → resolves ORM objects → calls TurnoService.crear_turno().
        Returns the created Turno serialised with TurnoSerializer (HTTP 201).

        ORM resolution:
            profesional_id and servicio_id from validated_data are resolved to
            model instances here (not in the serializer) because:
            1. Serializers should not make DB calls.
            2. The view owns the responsibility of ensuring objects exist and
               belong to the correct empresa (get_object_or_404 handles both).
        """
        serializer = CrearTurnoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Resolve FK IDs to model instances, 404 if not found or wrong empresa
        profesional = get_object_or_404(
            Profesional.objects.for_empresa(request.empresa),
            id=data["profesional_id"],
            activo=True,
        )
        servicio = get_object_or_404(
            Servicio.objects.for_empresa(request.empresa),
            id=data["servicio_id"],
            activo=True,
        )
        cliente = None
        if data.get("cliente_id"):
            # Import inline to avoid circular cross-module import at module level
            from modules.clientes.models import Cliente
            cliente = get_object_or_404(
                Cliente.objects.for_empresa(request.empresa),
                id=data["cliente_id"],
            )

        try:
            turno = TurnoService.crear_turno(
                empresa=request.empresa,
                profesional=profesional,
                servicio=servicio,
                fecha_inicio=data["fecha_inicio"],
                cliente=cliente,
                notas_cliente=data.get("notas_cliente", ""),
                notas_internas=data.get("notas_internas", ""),
                usuario=request.user,
            )
        except (DjangoValidationError, TurnoNoDisponibleError) as exc:
            return _handle_service_error(exc)

        # Re-fetch with select_related so the output serializer has all JOINs
        turno_completo = self._get_turno(turno.id)
        output = TurnoSerializer(turno_completo, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    # ── state transition actions ──────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="confirmar")
    def confirmar(self, request, pk=None):
        """
        POST /turnos/{id}/confirmar/

        Transitions estado: PENDIENTE → CONFIRMADO.
        Accepts optional precio_final to snapshot the price at confirmation time.
        """
        serializer = ConfirmarTurnoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        turno = self._get_turno(pk)
        try:
            turno = TurnoService.confirmar_turno(
                turno=turno,
                precio_final=serializer.validated_data.get("precio_final"),
                usuario=request.user,
            )
        except (DjangoValidationError, TransicionInvalidaError) as exc:
            return _handle_service_error(exc)

        output = TurnoSerializer(self._get_turno(turno.id), context={"request": request})
        return Response(output.data)

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        """
        POST /turnos/{id}/cancelar/

        Transitions estado: PENDIENTE → CANCELADO or CONFIRMADO → CANCELADO.
        cancelado_por is required. motivo is optional.
        """
        serializer = CancelarTurnoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        turno = self._get_turno(pk)
        try:
            turno = TurnoService.cancelar_turno(
                turno=turno,
                cancelado_por=serializer.validated_data["cancelado_por"],
                motivo=serializer.validated_data.get("motivo", ""),
                usuario=request.user,
            )
        except (DjangoValidationError, TransicionInvalidaError) as exc:
            return _handle_service_error(exc)

        output = TurnoSerializer(self._get_turno(turno.id), context={"request": request})
        return Response(output.data)

    @action(detail=True, methods=["post"], url_path="reprogramar")
    def reprogramar(self, request, pk=None):
        """
        POST /turnos/{id}/reprogramar/

        Moves the appointment to a new start time, keeping the original duration.
        Runs availability checks via TurnoService (which calls DisponibilidadService).
        """
        serializer = ReprogramarTurnoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        turno = self._get_turno(pk)
        try:
            turno = TurnoService.reprogramar_turno(
                turno=turno,
                nueva_fecha_inicio=serializer.validated_data["nueva_fecha_inicio"],
                usuario=request.user,
            )
        except (DjangoValidationError, TurnoNoDisponibleError, TransicionInvalidaError) as exc:
            return _handle_service_error(exc)

        output = TurnoSerializer(self._get_turno(turno.id), context={"request": request})
        return Response(output.data)

    @action(detail=True, methods=["post"], url_path="completar")
    def completar(self, request, pk=None):
        """
        POST /turnos/{id}/completar/

        Transitions estado: CONFIRMADO → COMPLETADO (terminal).
        Accepts an empty body {}.
        """
        serializer = MarcarCompletadoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        turno = self._get_turno(pk)
        try:
            turno = TurnoService.marcar_completado(turno=turno, usuario=request.user)
        except (DjangoValidationError, TransicionInvalidaError) as exc:
            return _handle_service_error(exc)

        output = TurnoSerializer(self._get_turno(turno.id), context={"request": request})
        return Response(output.data)

    @action(detail=True, methods=["post"], url_path="ausente")
    def ausente(self, request, pk=None):
        """
        POST /turnos/{id}/ausente/

        Transitions estado: CONFIRMADO → AUSENTE (terminal, client no-show).
        Accepts an empty body {}.
        """
        serializer = MarcarAusenteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        turno = self._get_turno(pk)
        try:
            turno = TurnoService.marcar_ausente(turno=turno, usuario=request.user)
        except (DjangoValidationError, TransicionInvalidaError) as exc:
            return _handle_service_error(exc)

        output = TurnoSerializer(self._get_turno(turno.id), context={"request": request})
        return Response(output.data)

    # ── slots endpoint ────────────────────────────────────────────────────────

    @action(detail=False, methods=["get"], url_path="slots")
    def slots(self, request):
        """
        GET /turnos/slots/?fecha=2024-01-15&servicio=<uuid>&profesional=<uuid>

        Returns all free appointment slots for a given day and service.
        The response is the booking calendar: a list of SlotDisponible objects
        that the front-end renders as clickable time blocks.

        Query params:
            fecha        (required) ISO date string: "2024-01-15"
            servicio     (required) UUID of the Servicio
            profesional  (optional) UUID — if omitted, returns slots for all
                         professionals who offer this service

        Query count: always 4 (O(1)) — see DisponibilidadService for proof.

        Returns 400 if required params are missing or fecha is not a valid date.
        Returns 200 with an empty list [] if no slots are available.
        """
        # ── Validate query params ─────────────────────────────────────────────
        fecha_str    = request.query_params.get("fecha")
        servicio_id  = request.query_params.get("servicio")

        errors = {}
        if not fecha_str:
            errors["fecha"] = "Este campo es requerido."
        if not servicio_id:
            errors["servicio"] = "Este campo es requerido."
        if errors:
            raise DRFValidationError(errors)

        try:
            fecha = date.fromisoformat(fecha_str)
        except ValueError:
            raise DRFValidationError(
                {"fecha": f"Formato de fecha inválido: '{fecha_str}'. Use YYYY-MM-DD."}
            )

        # ── Resolve objects ───────────────────────────────────────────────────
        servicio = get_object_or_404(
            Servicio.objects.for_empresa(request.empresa),
            id=servicio_id,
            activo=True,
        )

        profesional = None
        profesional_id = request.query_params.get("profesional")
        if profesional_id:
            profesional = get_object_or_404(
                Profesional.objects.for_empresa(request.empresa),
                id=profesional_id,
                activo=True,
            )

        # ── Call service (read-only, 4 queries) ───────────────────────────────
        slots = DisponibilidadService.obtener_slots_disponibles(
            empresa=request.empresa,
            fecha=fecha,
            servicio=servicio,
            profesional=profesional,
        )

        serializer = SlotDisponibleSerializer(slots, many=True)
        return Response({"count": len(slots), "slots": serializer.data})

    # ── private helpers ───────────────────────────────────────────────────────

    def _get_turno(self, pk) -> Turno:
        """
        Fetch a single Turno by PK, scoped to request.empresa, with all
        select_related joins applied.

        Used by retrieve() and all action methods so they get a fully-loaded
        instance without issuing separate queries for profesional/servicio/cliente.

        Returns HTTP 404 if the turno doesn't exist or belongs to another empresa.
        The empresa scope in the queryset prevents cross-tenant access without
        requiring an explicit permission check in each action method.
        """
        obj = get_object_or_404(
            _turno_queryset(self.request.empresa),
            id=pk,
        )
        self.check_object_permissions(self.request, obj)
        return obj
