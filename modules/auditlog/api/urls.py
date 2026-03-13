from django.urls import path, include
from rest_framework.routers import DefaultRouter
from modules.auditlog.api.views import AuditLogViewSet

router = DefaultRouter()
router.register(r"", AuditLogViewSet, basename="auditlog")

urlpatterns = [
    path("", include(router.urls)),
]
