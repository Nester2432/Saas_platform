"""
config/urls.py

Root URL configuration. All module URLs are registered here.

API versioning via URL prefix: /api/v1/
"""

from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView, TokenObtainPairView

from apps.usuarios.auth.serializers import CustomTokenObtainPairSerializer


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


urlpatterns = [
    # Django admin
    path("admin/", admin.site.urls),

    # Auth endpoints
    path("api/v1/", include("modules.users.urls")),
    path("api/v1/auth/token/",         CustomTokenObtainPairView.as_view(), name="token_obtain"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(),          name="token_refresh"),

    # Core apps
    path("api/v1/", include("apps.empresas.urls")),
    # path("api/v1/", include("apps.usuarios.urls")), # Removed to transition to modules.users
    path("api/v1/", include("apps.modulos.urls")),

    # Business modules
    path("api/v1/", include("modules.clientes.urls")),
    path("api/v1/", include("modules.turnos.urls")),
    path("api/v1/", include("modules.ventas.urls")),
    path("api/v1/pagos/", include("modules.pagos.urls")),
    path("api/v1/facturacion/", include("modules.facturacion.urls")),
    path("api/v1/", include("modules.inventario.urls")),
    path("api/v1/billing/", include("modules.billing.urls")),
    path("api/v1/payments/", include("modules.payments.urls")),
    path("api/v1/auditlog/", include("modules.auditlog.api.urls")),
    path("api/v1/events/", include("modules.events.urls")),
    
    # UI Views
    path("events/", include("modules.events.urls_ui")),
]
