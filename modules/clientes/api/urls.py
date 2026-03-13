"""
modules/clientes/urls.py

URL routing for the clientes module.

All routes are prefixed with /api/v1/ by config/urls.py.

Generated routes:
    GET    /api/v1/clientes/                           → ClienteViewSet.list
    POST   /api/v1/clientes/                           → ClienteViewSet.create
    GET    /api/v1/clientes/{id}/                      → ClienteViewSet.retrieve
    PATCH  /api/v1/clientes/{id}/                      → ClienteViewSet.partial_update
    DELETE /api/v1/clientes/{id}/                      → ClienteViewSet.destroy

    GET    /api/v1/clientes/{id}/notas/                → ClienteViewSet.notas (GET)
    POST   /api/v1/clientes/{id}/notas/                → ClienteViewSet.notas (POST)

    GET    /api/v1/clientes/{id}/etiquetas/            → ClienteViewSet.etiquetas (GET)
    POST   /api/v1/clientes/{id}/etiquetas/            → ClienteViewSet.etiquetas (POST)
    DELETE /api/v1/clientes/{id}/etiquetas/{eid}/      → ClienteViewSet.quitar_etiqueta

    GET    /api/v1/clientes/{id}/historial/            → ClienteViewSet.historial

    GET    /api/v1/etiquetas/                          → EtiquetaClienteViewSet.list
    POST   /api/v1/etiquetas/                          → EtiquetaClienteViewSet.create
    GET    /api/v1/etiquetas/{id}/                     → EtiquetaClienteViewSet.retrieve
    PATCH  /api/v1/etiquetas/{id}/                     → EtiquetaClienteViewSet.partial_update
    DELETE /api/v1/etiquetas/{id}/                     → EtiquetaClienteViewSet.destroy
"""

from rest_framework.routers import DefaultRouter
from modules.clientes.api.views import ClienteViewSet, EtiquetaClienteViewSet

router = DefaultRouter()
router.register(r"clientes", ClienteViewSet, basename="cliente")
router.register(r"etiquetas", EtiquetaClienteViewSet, basename="etiqueta-cliente")

urlpatterns = router.urls
