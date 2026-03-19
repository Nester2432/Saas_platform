from django.urls import path
from .demo_views import (
    DemoDashboardView, DemoClientesView, DemoVentasView, DemoInventarioView,
    DemoFacturacionView, DemoAgendaView, DemoBillingView, DemoEventosView,
    DemoContactosListView, DemoContactosDetailView
)

urlpatterns = [
    path("dashboard/",  DemoDashboardView.as_view(),  name="dashboard"),
    path("clientes/",   DemoClientesView.as_view(),   name="clientes"),
    path("ventas/",     DemoVentasView.as_view(),     name="ventas"),
    path("inventario/", DemoInventarioView.as_view(), name="inventario"),
    path("facturacion/", DemoFacturacionView.as_view(), name="facturacion"),
    path("agenda/",     DemoAgendaView.as_view(),     name="agenda"),
    path("billing/",    DemoBillingView.as_view(),    name="billing"),
    path("eventos/",    DemoEventosView.as_view(),    name="eventos"),
    path("contactos/",  DemoContactosListView.as_view(), name="contactos"),
    path("contactos/<uuid:pk>/", DemoContactosDetailView.as_view(), name="contacto-detalle"),
]
