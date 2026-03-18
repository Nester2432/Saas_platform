"""
modules/clientes/views.py

ViewSets for the clientes module.

Architecture contract:
- Views are THIN orchestrators: validate input → call service → return response
- NO business logic in views (no direct ORM, no validation beyond serializer)
- TenantQuerysetMixin handles empresa scoping and audit field injection
- Services handle all mutations, validations and historial events

Queryset optimizations:
- select_related for FKs used in serializer fields (created_by)
- prefetch_related for M2M (etiquetas) and reverse FKs (notas_detalle, historial)

Endpoints:
    GET    /clientes/                      → list
    POST   /clientes/                      → create
    GET    /clientes/{id}/                 → retrieve
    PATCH  /clientes/{id}/                 → partial update
    DELETE /clientes/{id}/                 → soft delete

    GET    /clientes/{id}/notas/           → list notes
    POST   /clientes/{id}/notas/           → add note

    GET    /clientes/{id}/etiquetas/       → list tags
    POST   /clientes/{id}/etiquetas/       → add tag
    DELETE /clientes/{id}/etiquetas/{eid}/ → remove tag

    GET    /clientes/{id}/historial/       → event history

    GET    /etiquetas/                     → list empresa tags
    POST   /etiquetas/                     → create tag
    PATCH  /etiquetas/{id}/               → update tag
    DELETE /etiquetas/{id}/               → soft delete tag
"""

import logging
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response

from core.mixins import TenantQuerysetMixin, AuditLogMixin
from core.pagination import DefaultPagination, SmallPagination
from core.permissions.base import IsTenantAuthenticated, ModuloActivoPermission
from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    NotaCliente,
    HistorialCliente,
)
from modules.clientes.api.permissions import ClienteObjectPermission
from modules.clientes.api.serializers import (
    ClienteSerializer,
    ClienteCreateSerializer,
    ClienteUpdateSerializer,
    EtiquetaClienteSerializer,
    EtiquetaClienteCreateSerializer,
    NotaClienteSerializer,
    NotaClienteCreateSerializer,
    HistorialClienteSerializer,
    AgregarEtiquetaSerializer,
)
from modules.clientes.api.serializers_crm import (
    ContactoListSerializer,
    Contacto360Serializer,
)
from modules.clientes.selectors_crm import (
    get_contactos_queryset,
    get_contacto_360,
)
from modules.clientes.services import ClienteService

logger = logging.getLogger(__name__)


class ClienteViewSet(TenantQuerysetMixin, AuditLogMixin, viewsets.ModelViewSet):
    """
    Full CRUD ViewSet for Cliente.

    Inherits from TenantQuerysetMixin:
    - get_queryset()    → auto-scoped to request.empresa
    - perform_create()  → injects empresa, created_by, updated_by
    - perform_update()  → injects updated_by
    - perform_destroy() → soft delete via instance.soft_delete()

    All mutations go through ClienteService to ensure:
    - email uniqueness validation
    - HistorialCliente registration
    """

    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission, ClienteObjectPermission]
    modulo_requerido = "clientes"
    pagination_class = DefaultPagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    # ------------------------------------------------------------------
    # Search & ordering
    # DRF SearchFilter uses ILIKE (case-insensitive) on PostgreSQL.
    # Query param: ?search=juan
    # All search fields hit columns covered by composite indexes on empresa.
    # ------------------------------------------------------------------
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]

    search_fields = [
        # "^" prefix → starts-with match (uses index efficiently)
        "^nombre",
        "^apellido",
        "^email",
        "^telefono",
        # No prefix on nombre/apellido also allows mid-string search via "="
        # Use "^" to keep queries sargable (index-friendly) in production
    ]

    ordering_fields = ["created_at", "nombre", "apellido"]
    ordering = ["apellido", "nombre"]  # default ordering — matches Meta.ordering

    def get_queryset(self):
        """
        Base queryset with related data prefetched for serializer efficiency.

        TenantQuerysetMixin.get_queryset() calls super() first, then applies
        .for_empresa() — so this queryset is always tenant-scoped before
        SearchFilter and OrderingFilter are applied by DRF.

        Filter execution order (all automatic, no view code needed):
            1. TenantQuerysetMixin  → .for_empresa(request.empresa)
            2. SearchFilter         → WHERE nombre ILIKE %q% OR email ILIKE %q% …
            3. OrderingFilter       → ORDER BY apellido, nombre (or ?ordering=)
            4. Pagination           → LIMIT / OFFSET

        Prefetches are applied after filtering — Django evaluates the final
        queryset lazily so prefetch_related only fetches rows that passed all filters.
        """
        return (
            Cliente.objects
            .select_related("created_by")
            .prefetch_related(
                "etiquetas",
            )
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ClienteCreateSerializer
        if self.action in ("update", "partial_update"):
            return ClienteUpdateSerializer
        return ClienteSerializer

    # ------------------------------------------------------------------
    # Standard CRUD — delegate to service
    # ------------------------------------------------------------------

    def perform_create(self, serializer):
        """
        Override TenantQuerysetMixin.perform_create to route through ClienteService.
        ClienteService.crear_cliente handles validation, plans check, historial and tags.
        """
        datos = dict(serializer.validated_data)
        etiqueta_ids = datos.pop("etiqueta_ids", [])

        cliente = ClienteService.crear_cliente(
            empresa=self.request.empresa,
            datos=datos,
            usuario=self.request.user,
            etiqueta_ids=etiqueta_ids,
        )

        # Attach the created instance so the response serializer can use it
        serializer.instance = cliente

    def perform_update(self, serializer):
        """Route partial updates through ClienteService."""
        ClienteService.actualizar_cliente(
            cliente=self.get_object(),
            datos=serializer.validated_data,
            usuario=self.request.user,
        )

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        # Return full representation using ClienteSerializer
        output = ClienteSerializer(serializer.instance, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        # Refresh from DB and return full representation
        instance.refresh_from_db()
        output = ClienteSerializer(instance, context={"request": request})
        return Response(output.data)

    # ------------------------------------------------------------------
    # Extra actions
    # ------------------------------------------------------------------

    @action(detail=True, methods=["get", "post"], url_path="notas")
    def notas(self, request, pk=None):
        """
        GET  /clientes/{id}/notas/  → list all notes for this client
        POST /clientes/{id}/notas/  → add a new note
        """
        cliente = self.get_object()

        if request.method == "GET":
            notas_qs = (
                NotaCliente.objects
                .for_empresa(request.empresa)
                .filter(cliente=cliente)
                .select_related("created_by")
                .order_by("-created_at")
            )
            paginator = SmallPagination()
            page = paginator.paginate_queryset(notas_qs, request, view=self)
            serializer = NotaClienteSerializer(page, many=True)
            return paginator.get_paginated_response(serializer.data)

        # POST
        serializer = NotaClienteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        nota = ClienteService.agregar_nota(
            cliente=cliente,
            contenido=serializer.validated_data["contenido"],
            usuario=request.user,
        )

        output = NotaClienteSerializer(nota)
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get", "post"], url_path="etiquetas")
    def etiquetas(self, request, pk=None):
        """
        GET  /clientes/{id}/etiquetas/  → list tags on this client
        POST /clientes/{id}/etiquetas/  → assign a tag
        """
        cliente = self.get_object()

        if request.method == "GET":
            etiquetas_qs = cliente.etiquetas.filter(deleted_at__isnull=True)
            serializer = EtiquetaClienteSerializer(etiquetas_qs, many=True)
            return Response(serializer.data)

        # POST
        serializer = AgregarEtiquetaSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        etiqueta = get_object_or_404(
            EtiquetaCliente.objects.for_empresa(request.empresa),
            id=serializer.validated_data["etiqueta_id"],
        )

        ClienteService.agregar_etiqueta(cliente, etiqueta, request.user)

        output = EtiquetaClienteSerializer(etiqueta)
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["delete"],
        url_path=r"etiquetas/(?P<etiqueta_id>[^/.]+)",
    )
    def quitar_etiqueta(self, request, pk=None, etiqueta_id=None):
        """
        DELETE /clientes/{id}/etiquetas/{etiqueta_id}/  → remove tag from client
        """
        cliente = self.get_object()
        etiqueta = get_object_or_404(
            EtiquetaCliente.objects.for_empresa(request.empresa),
            id=etiqueta_id,
        )

        ClienteService.quitar_etiqueta(cliente, etiqueta, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["get"], url_path="historial")
    def historial(self, request, pk=None):
        """
        GET /clientes/{id}/historial/  → immutable event log for this client
        """
        cliente = self.get_object()
        historial_qs = (
            HistorialCliente.objects
            .filter(empresa=request.empresa, cliente=cliente)
            .select_related("created_by")
            .order_by("-created_at")
        )
        paginator = SmallPagination()
        page = paginator.paginate_queryset(historial_qs, request, view=self)
        serializer = HistorialClienteSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class EtiquetaClienteViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD ViewSet for EtiquetaCliente (empresa-scoped tags).

    Tags are managed independently of clients — create tags first,
    then assign them to clients via ClienteViewSet.etiquetas action.
    """

    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido = "clientes"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_queryset(self):
        return EtiquetaCliente.objects.order_by("nombre")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return EtiquetaClienteCreateSerializer
        return EtiquetaClienteSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        output = EtiquetaClienteSerializer(serializer.instance, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

class ContactoViewSet(viewsets.ReadOnlyModelViewSet):
    """
    CRM-optimized ViewSet for Contactos (aggregated read layer over Cliente).
    
    Provides:
    - GET /contactos/     -> List with total_ventas, total_turnos and search.
    - GET /contactos/{id} -> Single API response with full 360 detail (ventas, turnos, etc).
    """
    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido = "clientes"
    pagination_class = DefaultPagination

    def get_queryset(self):
        search = self.request.query_params.get("search")
        ordering = self.request.query_params.get("ordering")
        return get_contactos_queryset(tenant=self.request.empresa, search=search, ordering=ordering)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return Contacto360Serializer
        return ContactoListSerializer

    def retrieve(self, request, *args, **kwargs):
        instance_id = kwargs.get("pk")
        data = get_contacto_360(cliente_id=instance_id, tenant=request.empresa)
        if not data:
            return Response({"error": "Contacto no encontrado"}, status=status.HTTP_404_NOT_FOUND)
            
        serializer = Contacto360Serializer(data, context={"request": request})
        return Response(serializer.data)
