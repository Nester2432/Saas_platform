from django.urls import path, include
from rest_framework.routers import DefaultRouter
from modules.facturacion.api.views import FacturaViewSet, PuntoVentaViewSet

router = DefaultRouter()
router.register(r"facturas", FacturaViewSet, basename="factura")
router.register(r"puntos-venta", PuntoVentaViewSet, basename="punto-venta")

urlpatterns = [
    path("", include(router.urls)),
]
