from rest_framework.routers import DefaultRouter
from modules.inventario.api.views import (
    CategoriaProductoViewSet, 
    ProductoViewSet, 
    MovimientoInventarioViewSet, 
    StockActualViewSet
)

router = DefaultRouter()
router.register(r"categorias", CategoriaProductoViewSet, basename="categoria")
router.register(r"productos", ProductoViewSet, basename="producto")
router.register(r"movimientos", MovimientoInventarioViewSet, basename="movimiento")
router.register(r"stock-actual", StockActualViewSet, basename="stock-actual")

urlpatterns = router.urls
