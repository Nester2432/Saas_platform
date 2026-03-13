from rest_framework import viewsets, mixins
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from django.db.models import F

from core.mixins import TenantQuerysetMixin, AuditLogMixin
from modules.inventario.models import CategoriaProducto, Producto, MovimientoStock
from modules.inventario.api.serializers import (
    CategoriaProductoSerializer, 
    ProductoSerializer, 
    StockActualSerializer, 
    MovimientoInventarioSerializer
)
from modules.inventario.api.permissions import InventarioObjectPermission, InventarioRolPermission
from core.permissions.base import IsTenantAuthenticated


class CategoriaProductoViewSet(TenantQuerysetMixin, AuditLogMixin, viewsets.ModelViewSet):
    """
    CRUD for product categories.
    """
    queryset = CategoriaProducto.objects.all()
    serializer_class = CategoriaProductoSerializer
    permission_classes = [IsTenantAuthenticated, InventarioRolPermission, InventarioObjectPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    search_fields = ["nombre"]
    ordering_fields = ["orden", "nombre"]
    ordering = ["orden", "nombre"]


class ProductoViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    CRUD for products. 
    stock_actual is strictly read-only and automatically filtered by tenant.
    """
    queryset = Producto.objects.select_related("categoria").all()
    serializer_class = ProductoSerializer
    permission_classes = [IsTenantAuthenticated, InventarioRolPermission, InventarioObjectPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["categoria", "activo", "permite_stock_negativo"]
    search_fields = ["nombre", "codigo"]
    ordering_fields = ["nombre", "codigo", "stock_actual"]
    ordering = ["nombre"]

    def perform_create(self, serializer):
        from modules.billing.services.billing_service import BillingService
        # Check product limit before creating
        BillingService.check_plan_limits(self.request.empresa, "productos")
        serializer.save(empresa=self.request.empresa)


class StockActualViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    Lightweight read-only endpoint for stock queries.
    Supports stock_bajo=true filtering.
    """
    queryset = Producto.objects.select_related("categoria").filter(activo=True)
    serializer_class = StockActualSerializer
    permission_classes = [IsTenantAuthenticated, InventarioRolPermission, InventarioObjectPermission]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["categoria"]
    search_fields = ["nombre", "codigo"]
    ordering_fields = ["stock_actual", "nombre"]
    ordering = ["nombre"]

    def get_queryset(self):
        qs = super().get_queryset()
        stock_bajo = self.request.query_params.get("stock_bajo", None)
        if stock_bajo and stock_bajo.lower() in ["true", "1", "yes"]:
            qs = qs.filter(stock_actual__lte=F("stock_minimo"))
        return qs


class MovimientoInventarioViewSet(TenantQuerysetMixin, 
                                  mixins.CreateModelMixin, 
                                  mixins.ListModelMixin, 
                                  mixins.RetrieveModelMixin, 
                                  viewsets.GenericViewSet):
    """
    Immutable ledger for inventory.
    Only supports Listing, Retrieving, and Creating movements.
    Update and Delete are intentionally omitted.
    Creation routes to MovimientoService to ensure atomic invariants.
    """
    queryset = MovimientoStock.objects.select_related("producto").all()
    serializer_class = MovimientoInventarioSerializer
    permission_classes = [InventarioObjectPermission]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["producto", "tipo"]
    ordering_fields = ["created_at"]
    ordering = ["-created_at"]
