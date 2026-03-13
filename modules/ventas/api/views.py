"""
modules/ventas/api/views.py

REST API for the ventas (sales) module.

Architecture contract:
    Views are THIN orchestrators. Each action method does exactly:
        1. Authenticate + authorise (permission_classes / get_permissions)
        2. Validate input (serializer.is_valid(raise_exception=True))
        3. Resolve UUIDs to ORM objects (get_object_or_404 calls)
        4. Call exactly ONE service method
        5. Serialise with VentaSerializer and return Response

    Views NEVER:
        - Access request.data after serializer validation
        - Set Venta.estado, Venta.total, or any model field directly
        - Call MovimientoService (that is VentaService's responsibility)
        - Execute business logic or state-machine transitions

Query budget (all O(1)):
    LIST    → COUNT + SELECT + 4 prefetch queries (paginated, empresa-scoped)
    DETAIL  → 1 SELECT + 4 prefetch queries
    ACTIONS → DETAIL queries + service writes (each service method is O(N lines))

Exception handling:
    Django's ValidationError is NOT a DRF exception — DRF's handler does not
    catch it automatically. _handle_service_error() converts all domain errors
    (ValidationError + ventas-specific exceptions) to structured HTTP responses
    matching the platform error envelope.

    HTTP status mapping:
        TransicionVentaInvalidaError  → 409 Conflict
        VentaSinLineasError           → 422 Unprocessable
        PagoInsuficienteError         → 422 Unprocessable
        DevolucionInvalidaError       → 422 Unprocessable
        StockInsuficienteError        → 409 Conflict (domain conflict)
        DjangoValidationError         → 400 Bad Request

Endpoints exposed:
    GET    /ventas/                        → list (filtered, paginated)
    POST   /ventas/                        → crear_venta (BORRADOR)
    GET    /ventas/{id}/                   → retrieve
    POST   /ventas/{id}/agregar_linea/     → VentaService.agregar_linea
    POST   /ventas/{id}/quitar_linea/      → VentaService.quitar_linea
    POST   /ventas/{id}/confirmar/         → VentaService.confirmar_venta
    POST   /ventas/{id}/cancelar/          → VentaService.cancelar_venta
    POST   /ventas/{id}/pagar/             → VentaService.registrar_pago
    POST   /ventas/{id}/devolver/          → VentaService.registrar_devolucion
"""

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404

from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from core.mixins import TenantQuerysetMixin
from core.pagination import DefaultPagination
from core.permissions.base import IsTenantAuthenticated, ModuloActivoPermission

from modules.inventario.exceptions import StockInsuficienteError
from modules.ventas.api.permissions import (
    PuedeVerVentas,
    PuedeCrearVentas,
    PuedeEditarVentas,
    PuedeConfirmarVentas,
    PuedeCancelarVentas,
    PuedePagarVentas,
    PuedeDevolverVentas,
    VentaObjectPermission,
)
from modules.ventas.api.serializers import (
    VentaSerializer,
    CrearVentaSerializer,
    AgregarLineaSerializer,
    QuitarLineaSerializer,
    ConfirmarVentaSerializer,
    CancelarVentaSerializer,
    RegistrarPagoSerializer,
    RegistrarDevolucionSerializer,
)
from modules.ventas.exceptions import (
    DevolucionInvalidaError,
    PagoInsuficienteError,
    TransicionVentaInvalidaError,
    VentaSinLineasError,
)
from modules.ventas.models import LineaVenta, MetodoPago, Venta
from modules.ventas.services import VentaService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared queryset builder
# ─────────────────────────────────────────────────────────────────────────────

def _venta_queryset(empresa):
    """
    Base queryset scoped to empresa, with all FK relations pre-loaded.

    SQL anatomy:
        SELECT ventas_venta.*
          JOIN clientes_cliente       (select_related, LEFT OUTER)
          JOIN usuarios_usuario       (select_related created_by, LEFT OUTER)
        + 4 prefetch queries (lineas→producto, pagos→metodo_pago,
                              devoluciones→lineas→linea_venta)

    Total: ~5 SQL statements regardless of how many lines/pagos a sale has.
    This function is the single authorised builder — used by get_queryset()
    (list/retrieve) and _get_venta() (all action methods).
    """
    return (
        Venta.objects
        .for_empresa(empresa)
        .select_related(
            "cliente",       # → ClienteResumenSerializer (nullable)
            "created_by",    # → audit
        )
        .prefetch_related(
            "lineas__producto",                  # → LineaVentaSerializer.producto
            "pagos__metodo_pago",                # → PagoVentaSerializer.metodo_pago
            "devoluciones__lineas__linea_venta", # → DevolucionVentaSerializer.lineas
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# Exception → HTTP response conversion
# ─────────────────────────────────────────────────────────────────────────────

def _handle_service_error(exc: Exception) -> Response:
    """
    Convert domain exceptions from VentaService into structured HTTP responses.

    All platform domain exceptions inherit from Django's ValidationError, which
    DRF's exception handler does NOT intercept automatically. We convert them
    here to ensure the response shape matches the platform error envelope.

    Status code rationale:
        409 Conflict:         TransicionVentaInvalidaError, StockInsuficienteError
            → The request is syntactically valid but conflicts with current resource state.
        422 Unprocessable:    VentaSinLineasError, PagoInsuficienteError, DevolucionInvalidaError
            → The request is valid JSON but the data fails domain rules.
        400 Bad Request:      DjangoValidationError (tenant mismatch, field errors)
            → Generic validation failures not covered by the above.
    """
    def _make_body(code: str, message: str, details: dict = None) -> dict:
        return {
            "error":   True,
            "code":    code,
            "message": message,
            "details": details or {},
        }

    if isinstance(exc, TransicionVentaInvalidaError):
        return Response(
            _make_body(
                code    = exc.code,
                message = exc.messages[0] if exc.messages else str(exc),
                details = {
                    "estado_actual":  exc.estado_actual,
                    "estado_destino": exc.estado_destino,
                },
            ),
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, StockInsuficienteError):
        return Response(
            _make_body(
                code    = "STOCK_INSUFICIENTE",
                message = exc.messages[0] if exc.messages else str(exc),
                details = {
                    "producto_id": str(exc.producto.id) if hasattr(exc, "producto") else None,
                    "disponible":  exc.disponible if hasattr(exc, "disponible") else None,
                    "solicitado":  exc.solicitado if hasattr(exc, "solicitado") else None,
                },
            ),
            status=status.HTTP_409_CONFLICT,
        )

    if isinstance(exc, VentaSinLineasError):
        return Response(
            _make_body(
                code    = exc.code,
                message = exc.messages[0] if exc.messages else str(exc),
            ),
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if isinstance(exc, PagoInsuficienteError):
        return Response(
            _make_body(
                code    = exc.code,
                message = exc.messages[0] if exc.messages else str(exc),
                details = {
                    "total":    str(exc.total),
                    "pagado":   str(exc.pagado),
                    "faltante": str(exc.faltante),
                },
            ),
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if isinstance(exc, DevolucionInvalidaError):
        return Response(
            _make_body(
                code    = exc.code,
                message = exc.messages[0] if exc.messages else str(exc),
            ),
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if isinstance(exc, DjangoValidationError):
        messages = exc.messages if hasattr(exc, "messages") else [str(exc)]
        return Response(
            _make_body(
                code    = getattr(exc, "code", "validation_error") or "validation_error",
                message = messages[0] if messages else "Error de validación.",
                details = {"non_field_errors": messages},
            ),
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Unexpected — re-raise for DRF's 500 handler
    raise exc


# ─────────────────────────────────────────────────────────────────────────────
# VentaViewSet
# ─────────────────────────────────────────────────────────────────────────────

class VentaViewSet(
    TenantQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    ViewSet for the full Venta lifecycle.

    Inherits GenericViewSet + explicit mixins (not ModelViewSet) because:
        - create → VentaService, not serializer.save()
        - update / partial_update → not exposed (use action endpoints)
        - destroy → not exposed (use cancelar instead)

    list and retrieve are included via ListModelMixin + RetrieveModelMixin.
    They use get_queryset() which is provided by TenantQuerysetMixin.

    Permission layers:
        Layer 1 (all requests): IsTenantAuthenticated + ModuloActivoPermission
        Layer 2 (per action):   granular permission from permissions.py via get_permissions()
        Layer 3 (per object):   VentaObjectPermission via get_permissions()

    Filter params (all optional, combinable):
        ?estado=<EstadoVenta>      BORRADOR | CONFIRMADA | PAGADA | CANCELADA | DEVUELTA
        ?cliente=<uuid>            filter by cliente FK
        ?fecha_desde=<date>        sales on or after this date (ISO 8601)
        ?fecha_hasta=<date>        sales on or before this date (ISO 8601)
        ?search=<str>              search in numero, datos_cliente JSON
        ?ordering=<field>          fecha (default desc), total, numero
    """

    serializer_class    = VentaSerializer
    pagination_class    = DefaultPagination
    permission_classes  = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido    = "ventas"
    filter_backends     = [filters.SearchFilter, filters.OrderingFilter]
    search_fields       = ["numero", "datos_cliente", "notas"]
    ordering_fields     = ["fecha", "total", "numero", "created_at"]
    ordering            = ["-fecha"]

    # ── Queryset ──────────────────────────────────────────────────────────────

    def get_queryset(self):
        """
        Base queryset — always scoped to request.empresa.

        TenantQuerysetMixin.get_queryset() is the primary tenant guard.
        _venta_queryset() adds select_related + prefetch_related on top.

        Filter params applied manually below (custom fields not covered by
        DRF's built-in FilterBackend).
        """
        qs = _venta_queryset(self.request.empresa)

        estado      = self.request.query_params.get("estado")
        cliente     = self.request.query_params.get("cliente")
        fecha_desde = self.request.query_params.get("fecha_desde")
        fecha_hasta = self.request.query_params.get("fecha_hasta")

        if estado:
            qs = qs.filter(estado=estado)
        if cliente:
            qs = qs.filter(cliente_id=cliente)
        if fecha_desde:
            qs = qs.filter(fecha__date__gte=fecha_desde)
        if fecha_hasta:
            qs = qs.filter(fecha__date__lte=fecha_hasta)

        return qs

    # ── Permissions ───────────────────────────────────────────────────────────

    def get_permissions(self):
        """
        Map actions to permission classes.

        Layer 1 (always): [IsTenantAuthenticated, ModuloActivoPermission]
        Layer 2 (action): action-specific class from permissions.py
        Layer 3 (object): VentaObjectPermission on detail endpoints
        """
        base   = [IsTenantAuthenticated(), ModuloActivoPermission()]
        object_perm = [VentaObjectPermission()]

        action_perms = {
            "list":          [PuedeVerVentas()],
            "retrieve":      [PuedeVerVentas()] + object_perm,
            "create":        [PuedeCrearVentas()],
            "agregar_linea": [PuedeEditarVentas()] + object_perm,
            "items":         [PuedeEditarVentas()] + object_perm,  # alias
            "quitar_linea":  [PuedeEditarVentas()] + object_perm,
            "confirmar":     [PuedeConfirmarVentas()] + object_perm,
            "cancelar":      [PuedeCancelarVentas()] + object_perm,
            "pagar":         [PuedePagarVentas()] + object_perm,
            "devolver":      [PuedeDevolverVentas()] + object_perm,
        }
        return base + action_perms.get(self.action, [PuedeVerVentas()])

    # ── Serializer selection ──────────────────────────────────────────────────

    def get_serializer_class(self):
        """
        Return the appropriate serializer for each action.

        Read actions → VentaSerializer (rich nested output).
        Write actions → specific input serializer.
        """
        write_map = {
            "create":        CrearVentaSerializer,
            "agregar_linea": AgregarLineaSerializer,
            "items":         AgregarLineaSerializer,  # alias
            "quitar_linea":  QuitarLineaSerializer,
            "confirmar":     ConfirmarVentaSerializer,
            "cancelar":      CancelarVentaSerializer,
            "pagar":         RegistrarPagoSerializer,
            "devolver":      RegistrarDevolucionSerializer,
        }
        return write_map.get(self.action, VentaSerializer)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_venta(self, pk) -> Venta:
        """
        Fetch a single Venta by PK, scoped to request.empresa, with all
        select_related and prefetch_related applied.

        Used by retrieve() override and all action methods. Returns HTTP 404
        if the venta doesn't exist or belongs to another empresa.
        """
        return get_object_or_404(
            _venta_queryset(self.request.empresa), id=pk
        )

    def _venta_response(self, venta: Venta, http_status=status.HTTP_200_OK) -> Response:
        """
        Re-fetch a Venta with full prefetches and serialise.

        After any write operation, the venta instance returned by the service
        may have stale reverse-relation caches (e.g. lineas, pagos) because
        the prefetch_related on the original instance was done before the write.
        Re-fetching from DB ensures the response reflects the post-write state.
        """
        refreshed = self._get_venta(venta.id)
        serializer = VentaSerializer(refreshed, context={"request": self.request})
        return Response(serializer.data, status=http_status)

    def _resolve_metodo_pago(self, metodo_pago_id) -> MetodoPago:
        """Resolve a MetodoPago UUID to an ORM instance, tenant-scoped."""
        return get_object_or_404(
            MetodoPago.objects.filter(
                empresa=self.request.empresa,
                activo=True,
                deleted_at__isnull=True,
            ),
            id=metodo_pago_id,
        )

    def _resolve_linea(self, linea_id, venta: Venta) -> LineaVenta:
        """Resolve a LineaVenta UUID to an ORM instance, scoped to venta."""
        return get_object_or_404(
            LineaVenta.objects.filter(
                empresa=self.request.empresa,
                venta=venta,
                deleted_at__isnull=True,
            ),
            id=linea_id,
        )

    # ── CRUD endpoints ────────────────────────────────────────────────────────

    def create(self, request, *args, **kwargs):
        """
        POST /ventas/

        Creates a Venta in BORRADOR state via VentaService.crear_venta().
        No lines are added — use /agregar_linea/ after creation.
        Returns 201 with the full VentaSerializer representation.
        """
        serializer = CrearVentaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Resolve optional FK references
        cliente = None
        if data.get("cliente_id"):
            from modules.clientes.models import Cliente
            cliente = get_object_or_404(
                Cliente.objects.for_empresa(request.empresa), id=data["cliente_id"]
            )

        turno = None
        if data.get("turno_id"):
            from modules.turnos.models import Turno
            turno = get_object_or_404(
                Turno.objects.for_empresa(request.empresa), id=data["turno_id"]
            )

        try:
            venta = VentaService.crear_venta(
                empresa         = request.empresa,
                cliente         = cliente,
                turno           = turno,
                descuento_total = data["descuento_total"],
                pago_diferido   = data["pago_diferido"],
                notas           = data["notas"],
                usuario         = request.user,
            )
        except DjangoValidationError as exc:
            return _handle_service_error(exc)

        return self._venta_response(venta, http_status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, pk=None, **kwargs):
        """
        GET /ventas/{id}/

        Returns the full VentaSerializer representation.
        Permission: ventas.ver + VentaObjectPermission.
        """
        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)
        serializer = VentaSerializer(venta, context={"request": request})
        return Response(serializer.data)

    # ── BORRADOR editing actions ──────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="agregar_linea")
    def agregar_linea(self, request, pk=None):
        """
        POST /ventas/{id}/agregar_linea/

        Adds a line item to a BORRADOR sale. Recalculates totals.
        Returns the full updated Venta representation.
        """
        serializer = AgregarLineaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)

        # Resolve optional producto FK
        producto = None
        if data.get("producto_id"):
            from modules.inventario.models import Producto
            producto = get_object_or_404(
                Producto.objects.for_empresa(request.empresa),
                id=data["producto_id"],
                activo=True,
            )

        try:
            VentaService.agregar_linea(
                empresa         = request.empresa,
                venta           = venta,
                producto        = producto,
                descripcion     = data.get("descripcion", ""),
                precio_unitario = data.get("precio_unitario"),
                cantidad        = data["cantidad"],
                descuento       = data["descuento"],
                usuario         = request.user,
            )
        except (DjangoValidationError, TransicionVentaInvalidaError) as exc:
            return _handle_service_error(exc)

        return self._venta_response(venta)

    @action(detail=True, methods=["post"], url_path="items")
    def items(self, request, pk=None):
        """
        POST /ventas/{id}/items/

        REST-friendly alias for agregar_linea.
        Adds a line item to a BORRADOR sale.
        """
        return self.agregar_linea(request, pk=pk)

    @action(detail=True, methods=["post"], url_path="quitar_linea")
    def quitar_linea(self, request, pk=None):
        """
        POST /ventas/{id}/quitar_linea/

        Removes a line item from a BORRADOR sale. Recalculates totals.
        Returns the full updated Venta representation.
        """
        serializer = QuitarLineaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)
        linea = self._resolve_linea(serializer.validated_data["linea_id"], venta)

        try:
            VentaService.quitar_linea(
                empresa = request.empresa,
                venta   = venta,
                linea   = linea,
                usuario = request.user,
            )
        except (DjangoValidationError, TransicionVentaInvalidaError) as exc:
            return _handle_service_error(exc)

        return self._venta_response(venta)

    # ── State transition actions ──────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="confirmar")
    def confirmar(self, request, pk=None):
        """
        POST /ventas/{id}/confirmar/

        Transitions BORRADOR → CONFIRMADA (or PAGADA if fully paid).
        Reduces stock via MovimientoService for all product lines.
        Assigns the correlative number via SecuenciaVenta.
        Returns the updated Venta.

        Request body:
            {
                "pagos": [
                    {"metodo_pago_id": "<uuid>", "monto": 1000.00, "referencia": ""},
                    ...
                ]
            }

        Empty pagos is allowed when Venta.pago_diferido=True.
        """
        serializer = ConfirmarVentaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)

        # Resolve each pago metodo_pago_id to a MetodoPago ORM instance
        pagos_resueltos = []
        for pago_data in serializer.validated_data.get("pagos", []):
            metodo = self._resolve_metodo_pago(pago_data["metodo_pago_id"])
            pagos_resueltos.append({
                "metodo_pago": metodo,
                "monto":       pago_data["monto"],
                "referencia":  pago_data.get("referencia", ""),
            })

        try:
            venta = VentaService.confirmar_venta(
                empresa = request.empresa,
                venta   = venta,
                pagos   = pagos_resueltos,
                usuario = request.user,
            )
        except (
            DjangoValidationError,
            TransicionVentaInvalidaError,
            VentaSinLineasError,
            PagoInsuficienteError,
            StockInsuficienteError,
        ) as exc:
            return _handle_service_error(exc)

        logger.info(
            "API CONFIRMAR: empresa=%s venta=%s numero=%s user=%s",
            request.empresa.id, venta.id, venta.numero, request.user.id,
        )
        return self._venta_response(venta)

    @action(detail=True, methods=["post"], url_path="cancelar")
    def cancelar(self, request, pk=None):
        """
        POST /ventas/{id}/cancelar/

        Cancels a BORRADOR, CONFIRMADA, or PAGADA sale.
        For CONFIRMADA/PAGADA: restores stock via MovimientoService.
        Returns the updated Venta (estado=CANCELADA).

        Request body:
            {"motivo": "Cliente solicitó cancelación."}
        """
        serializer = CancelarVentaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)

        try:
            venta = VentaService.cancelar_venta(
                empresa = request.empresa,
                venta   = venta,
                motivo  = serializer.validated_data.get("motivo", ""),
                usuario = request.user,
            )
        except (
            DjangoValidationError,
            TransicionVentaInvalidaError,
        ) as exc:
            return _handle_service_error(exc)

        logger.info(
            "API CANCELAR: empresa=%s venta=%s numero=%s user=%s",
            request.empresa.id, venta.id, venta.numero, request.user.id,
        )
        return self._venta_response(venta)

    @action(detail=True, methods=["post"], url_path="pagar")
    def pagar(self, request, pk=None):
        """
        POST /ventas/{id}/pagar/

        Registers a payment against a CONFIRMADA credit/account sale.
        When cumulative payments reach Venta.total, estado → PAGADA.
        Returns the updated Venta.

        Request body:
            {
                "metodo_pago_id": "<uuid>",
                "monto": 500.00,
                "referencia": ""
            }
        """
        serializer = RegistrarPagoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)
        metodo = self._resolve_metodo_pago(data["metodo_pago_id"])

        try:
            VentaService.registrar_pago(
                empresa     = request.empresa,
                venta       = venta,
                metodo_pago = metodo,
                monto       = data["monto"],
                referencia  = data.get("referencia", ""),
                usuario     = request.user,
            )
        except (DjangoValidationError, TransicionVentaInvalidaError) as exc:
            return _handle_service_error(exc)

        return self._venta_response(venta)

    @action(detail=True, methods=["post"], url_path="devolver")
    def devolver(self, request, pk=None):
        """
        POST /ventas/{id}/devolver/

        Registers a partial or total return against a CONFIRMADA or PAGADA sale.
        Restores stock for returned product lines via MovimientoService.
        If all items are fully returned, estado → DEVUELTA.

        Request body:
            {
                "items": [
                    {"linea_id": "<uuid>", "cantidad": 3},
                    ...
                ],
                "motivo": "Producto defectuoso.",
                "notas": ""
            }
        """
        serializer = RegistrarDevolucionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        venta = self._get_venta(pk)
        self.check_object_permissions(request, venta)

        # Resolve each linea_id to a LineaVenta ORM instance
        items_resueltos = []
        for item in data["items"]:
            linea = self._resolve_linea(item["linea_id"], venta)
            items_resueltos.append({
                "linea_venta": linea,
                "cantidad":    item["cantidad"],
            })

        try:
            VentaService.registrar_devolucion(
                empresa = request.empresa,
                venta   = venta,
                items   = items_resueltos,
                motivo  = data["motivo"],
                usuario = request.user,
            )
        except (
            DjangoValidationError,
            TransicionVentaInvalidaError,
            DevolucionInvalidaError,
        ) as exc:
            return _handle_service_error(exc)

        return self._venta_response(venta)


# ─────────────────────────────────────────────────────────────────────────────
# MetodoPagoViewSet — payment method catalogue
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework import serializers as drf_serializers  # noqa: E402


class MetodoPagoWriteSerializer(drf_serializers.ModelSerializer):
    """Input serializer for creating/updating MetodoPago instances."""
    class Meta:
        model  = MetodoPago
        fields = ["nombre", "tipo", "activo", "acepta_vuelto", "orden"]


class MetodoPagoViewSet(
    TenantQuerysetMixin,
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    CRUD for MetodoPago (payment method catalogue).

    GET    /metodos-pago/        → list all methods for this empresa
    POST   /metodos-pago/        → create (admin only)
    GET    /metodos-pago/{id}/   → retrieve
    PATCH  /metodos-pago/{id}/   → update (admin only)
    DELETE /metodos-pago/{id}/   → soft-delete (admin only)

    List is intentionally not restricted to activo=True so admins can manage
    inactive methods. The POS UI should filter by activo on the client side.

    Permissions: list/retrieve → ventas.ver; write → admin_empresa only.
    Payment method configuration is a setup task, not a daily operation.
    """

    serializer_class    = MetodoPagoWriteSerializer
    pagination_class    = DefaultPagination
    permission_classes  = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido    = "ventas"
    filter_backends     = [filters.OrderingFilter]
    ordering_fields     = ["orden", "nombre"]
    ordering            = ["orden", "nombre"]

    def get_queryset(self):
        return (
            MetodoPago.objects
            .for_empresa(self.request.empresa)
            .filter(deleted_at__isnull=True)
        )

    def get_permissions(self):
        from core.permissions.base import IsEmpresaAdmin
        base = [IsTenantAuthenticated(), ModuloActivoPermission()]
        if self.action in ("list", "retrieve"):
            return base + [PuedeVerVentas()]
        return base + [IsEmpresaAdmin()]

    def perform_create(self, serializer):
        serializer.save(
            empresa    = self.request.empresa,
            created_by = self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)