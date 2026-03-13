from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.core.exceptions import ValidationError as DjangoValidationError

from core.mixins import TenantQuerysetMixin
from core.permissions.base import IsTenantAuthenticated, ModuloActivoPermission
from modules.pagos.models import Pago
from modules.pagos.services.pagos import PagoService
from modules.pagos.api.serializers import PagoSerializer, RegistrarPagoSerializer
from modules.pagos.exceptions import PagosError
from modules.ventas.models import Venta, MetodoPago

class PagoViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for tracking and managing payment transactions.
    """
    queryset = Pago.objects.all()
    serializer_class = PagoSerializer
    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido = "ventas" # Use ventas module for now as payments depend on it
    filterset_fields = ["venta", "estado", "metodo_pago"]
    search_fields = ["referencia_externa"]
    ordering_fields = ["created_at", "monto"]
    ordering = ["-created_at"]

    def create(self, request, *args, **kwargs):
        """POST /pagos/ - Register a new payment intent."""
        serializer = RegistrarPagoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        venta = get_object_or_404(Venta.objects.for_empresa(request.empresa), id=data["venta_id"])
        metodo = get_object_or_404(MetodoPago.objects.for_empresa(request.empresa), id=data["metodo_pago_id"])
        
        try:
            pago = PagoService.registrar_pago(
                empresa=request.empresa,
                venta=venta,
                monto=data["monto"],
                metodo_pago=metodo,
                moneda=data["moneda"],
                referencia_externa=data.get("referencia_externa", ""),
                usuario=request.user
            )
        except DjangoValidationError as e:
            return Response({"error": True, "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            
        return Response(PagoSerializer(pago).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirmar(self, request, pk=None):
        """POST /pagos/{id}/confirmar/"""
        pago = self.get_object()
        try:
            pago = PagoService.confirmar_pago(request.empresa, pago, usuario=request.user)
        except PagosError as e:
            return Response({"error": True, "code": e.code, "message": str(e)}, status=status.HTTP_409_CONFLICT)
        except DjangoValidationError as e:
            return Response({"error": True, "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            
        return Response(PagoSerializer(pago).data)

    @action(detail=True, methods=["post"])
    def fallar(self, request, pk=None):
        """POST /pagos/{id}/fallar/"""
        pago = self.get_object()
        try:
            pago = PagoService.fallar_pago(request.empresa, pago, usuario=request.user)
        except PagosError as e:
            return Response({"error": True, "message": str(e)}, status=status.HTTP_409_CONFLICT)
            
        return Response(PagoSerializer(pago).data)

    @action(detail=True, methods=["post"])
    def reembolsar(self, request, pk=None):
        """POST /pagos/{id}/reembolsar/"""
        pago = self.get_object()
        try:
            pago = PagoService.reembolsar_pago(request.empresa, pago, usuario=request.user)
        except PagosError as e:
            return Response({"error": True, "message": str(e)}, status=status.HTTP_409_CONFLICT)
            
        return Response(PagoSerializer(pago).data)
