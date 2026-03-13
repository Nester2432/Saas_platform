from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError
from core.mixins import TenantQuerysetMixin
from modules.facturacion.models import Factura, EstadoFactura, PuntoVenta
from modules.facturacion.api.serializers import FacturaSerializer, PuntoVentaSerializer
from modules.facturacion.services.facturacion import FacturaService
from modules.facturacion.exceptions import FacturacionError

class PuntoVentaViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for PuntoVenta management.
    """
    queryset = PuntoVenta.objects.all()
    serializer_class = PuntoVentaSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["activo", "codigo"]
    ordering_fields = ["codigo", "created_at"]

class FacturaViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for Factura management.
    """
    queryset = Factura.objects.all()
    serializer_class = FacturaSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["venta", "estado", "tipo"]
    ordering_fields = ["created_at", "numero", "fecha_emision"]
    
    def get_queryset(self):
        return super().get_queryset().prefetch_related("lineas")

    @action(detail=False, methods=["post"], url_path="generar-desde-venta/(?P<venta_id>[^/.]+)")
    def generar_desde_venta(self, request, venta_id=None):
        """
        Custom action to generate a draft invoice from a sale ID.
        """
        from modules.ventas.models import Venta
        venta = get_object_or_404(Venta, id=venta_id, empresa=request.empresa)
        
        try:
            factura = FacturaService.generar_factura_desde_venta(
                request.empresa, venta, usuario=request.user
            )
            serializer = self.get_serializer(factura)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except FacturacionError as e:
            return Response(
                {"detail": str(e), "code": getattr(e, "code", "facturacion_error")},
                status=status.HTTP_409_CONFLICT
            )
        except Exception as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"])
    def emitir(self, request, pk=None):
        """
        Custom action to issue a draft invoice.
        Requires 'punto_venta_id' in the request body.
        """
        factura = self.get_object()
        punto_venta_id = request.data.get("punto_venta_id")
        
        if not punto_venta_id:
            return Response(
                {"detail": "El campo 'punto_venta_id' es obligatorio para emitir la factura."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        punto_venta = get_object_or_404(PuntoVenta, id=punto_venta_id, empresa=request.empresa)
        
        try:
            factura = FacturaService.emitir_factura(
                request.empresa, factura, punto_venta, usuario=request.user
            )
            serializer = self.get_serializer(factura)
            return Response(serializer.data)
        except (FacturacionError, ValidationError) as e:
            return Response(
                {"detail": str(e), "code": getattr(e, "code", "facturacion_error")},
                status=status.HTTP_409_CONFLICT
            )

    @action(detail=True, methods=["post"])
    def anular(self, request, pk=None):
        """
        Custom action to void an invoice.
        """
        factura = self.get_object()
        factura = FacturaService.anular_factura(
            request.empresa, factura, usuario=request.user
        )
        serializer = self.get_serializer(factura)
        return Response(serializer.data)
