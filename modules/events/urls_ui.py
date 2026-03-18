from django.urls import path
from .demo_views import (
    DemoDashboardView, DemoClientesView, DemoVentasView, DemoInventarioView,
    DemoFacturacionView, DemoAgendaView, DemoBillingView, DemoEventosView,
    DemoContactosListView, DemoContactosDetailView
)

urlpatterns = [
    path("demo/dashboard/",  DemoDashboardView.as_view(),  name="demo-dashboard"),
    path("demo/clientes/",   DemoClientesView.as_view(),   name="demo-clientes"),
    path("demo/ventas/",     DemoVentasView.as_view(),     name="demo-ventas"),
    path("demo/inventario/", DemoInventarioView.as_view(), name="demo-inventario"),
    path("demo/facturacion/", DemoFacturacionView.as_view(), name="demo-facturacion"),
    path("demo/agenda/",     DemoAgendaView.as_view(),     name="demo-agenda"),
    path("demo/billing/",    DemoBillingView.as_view(),    name="demo-billing"),
    path("demo/eventos/",    DemoEventosView.as_view(),    name="demo-eventos"),
    path("demo/contactos/",  DemoContactosListView.as_view(), name="demo-contactos"),
    path("demo/contactos/<uuid:pk>/", DemoContactosDetailView.as_view(), name="demo-contacto-detalle"),
]
