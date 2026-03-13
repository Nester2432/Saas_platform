"""
modules/ventas/api/urls.py

URL routing for the ventas module.

All routes are prefixed with /api/v1/ by config/urls.py.

Generated routes:

    ── Venta CRUD ──────────────────────────────────────────────────────────────
    GET    /api/v1/ventas/                    → VentaViewSet.list
    POST   /api/v1/ventas/                    → VentaViewSet.create (BORRADOR)
    GET    /api/v1/ventas/{id}/               → VentaViewSet.retrieve

    ── BORRADOR editing ────────────────────────────────────────────────────────
    POST   /api/v1/ventas/{id}/agregar_linea/ → VentaViewSet.agregar_linea
    POST   /api/v1/ventas/{id}/quitar_linea/  → VentaViewSet.quitar_linea

    ── State transitions ────────────────────────────────────────────────────────
    POST   /api/v1/ventas/{id}/confirmar/     → VentaViewSet.confirmar
    POST   /api/v1/ventas/{id}/cancelar/      → VentaViewSet.cancelar
    POST   /api/v1/ventas/{id}/pagar/         → VentaViewSet.pagar
    POST   /api/v1/ventas/{id}/devolver/      → VentaViewSet.devolver

    ── MetodoPago catalogue ────────────────────────────────────────────────────
    GET    /api/v1/metodos-pago/              → MetodoPagoViewSet.list
    POST   /api/v1/metodos-pago/              → MetodoPagoViewSet.create
    GET    /api/v1/metodos-pago/{id}/         → MetodoPagoViewSet.retrieve
    PATCH  /api/v1/metodos-pago/{id}/         → MetodoPagoViewSet.partial_update
    DELETE /api/v1/metodos-pago/{id}/         → MetodoPagoViewSet.destroy

Note on DELETE for ventas:
    Ventas are never deleted — use cancelar (POST /{id}/cancelar/) instead.
    Exposing DELETE would bypass VentaService and leave stock in an inconsistent
    state. The router's delete route is intentionally not registered.

Note on PUT/PATCH for ventas:
    Line-item editing is through agregar_linea / quitar_linea actions.
    There is no generic PATCH endpoint for Venta because field-level updates
    (e.g. changing notas or descuento_total on a BORRADOR) would need to go
    through service validation. These endpoints can be added later if needed.
"""

from rest_framework.routers import DefaultRouter

from modules.ventas.api.views import MetodoPagoViewSet, VentaViewSet

router = DefaultRouter()
router.register(r"ventas",        VentaViewSet,       basename="venta")
router.register(r"metodos-pago",  MetodoPagoViewSet,  basename="metodo-pago")

urlpatterns = router.urls