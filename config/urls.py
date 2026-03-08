"""
config/urls.py

Root URL configuration. All module URLs are registered here.

API versioning via URL prefix: /api/v1/
"""

from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView
from apps.usuarios.auth.serializers import CustomTokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


urlpatterns = [
    # Django admin
    path("admin/", admin.site.urls),

    # Auth endpoints (no tenant required)
    path("api/v1/auth/token/", CustomTokenObtainPairView.as_view(), name="token_obtain"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),

    # Core apps
    path("api/v1/", include("apps.empresas.urls")),
    path("api/v1/", include("apps.usuarios.urls")),
    path("api/v1/", include("apps.modulos.urls")),

    # Business modules
    path("api/v1/", include("modules.clientes.urls")),
    path("api/v1/", include("modules.turnos.urls")),
    path("api/v1/", include("modules.ventas.urls")),
    path("api/v1/", include("modules.inventario.urls")),
    path("api/v1/", include("modules.facturacion.urls")),
    path("api/v1/", include("modules.notificaciones.urls")),
    path("api/v1/", include("modules.reportes.urls")),
]
    path("api/v1/", include("modules.inventario.urls")),
    path("api/v1/", include("modules.facturacion.urls")),
    path("api/v1/", include("modules.notificaciones.urls")),
    path("api/v1/", include("modules.reportes.urls")),
]
