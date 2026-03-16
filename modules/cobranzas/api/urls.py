from django.urls import path, include
from rest_framework.routers import DefaultRouter
from modules.cobranzas.api.views import PagoViewSet

router = DefaultRouter()
router.register(r"", PagoViewSet, basename="pago")

urlpatterns = [
    path("", include(router.urls)),
]
