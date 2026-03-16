from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import EventStoreViewSet
from .demo_views import (
    DemoFullFlowView, DemoResourcesView, DemoStatusView, DemoActionView,
    DemoDashboardView, DemoClientesView, DemoVentasView, DemoInventarioView,
    DemoFacturacionView, DemoAgendaView, DemoBillingView, DemoEventosView
)

router = DefaultRouter()
router.register(r"event-store", EventStoreViewSet, basename="event-store")

urlpatterns = [
    path("", include(router.urls)),
    path("demo/full-flow/", DemoFullFlowView.as_view(), name="demo-full-flow"),
    path("demo/resources/", DemoResourcesView.as_view(), name="demo-resources"),
    path("demo/status/", DemoStatusView.as_view(), name="demo-status"),
    path("demo/action/", DemoActionView.as_view(), name="demo-action"),
    
    # UI Screens
    path("demo/dashboard/", DemoDashboardView.as_view(), name="demo-ui-dashboard"),
    path("demo/clientes/", DemoClientesView.as_view(), name="demo-ui-clientes"),
    path("demo/ventas/", DemoVentasView.as_view(), name="demo-ui-ventas"),
    path("demo/inventario/", DemoInventarioView.as_view(), name="demo-ui-inventario"),
    path("demo/facturacion/", DemoFacturacionView.as_view(), name="demo-ui-facturacion"),
    path("demo/agenda/", DemoAgendaView.as_view(), name="demo-ui-agenda"),
    path("demo/billing/", DemoBillingView.as_view(), name="demo-ui-billing"),
    path("demo/eventos/", DemoEventosView.as_view(), name="demo-ui-eventos"),
]
