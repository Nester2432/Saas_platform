from rest_framework import viewsets, permissions, filters
from django_filters.rest_framework import DjangoFilterBackend

from core.mixins import TenantQuerysetMixin
from core.permissions.base import IsEmpresaAdmin
from modules.auditlog.models import AuditLog
from modules.auditlog.api.serializers import AuditLogSerializer

class AuditLogViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    ReadOnly ViewSet for Audit Logs.
    Restricted to Empresa Admins or SuperAdmins.
    Supports filtering by user, action, and date range.
    """
    queryset = AuditLog.objects.select_related("usuario").all()
    serializer_class = AuditLogSerializer
    permission_classes = [IsEmpresaAdmin]
    
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = {
        "usuario": ["exact"],
        "accion": ["exact", "icontains"],
        "recurso": ["exact"],
        "creado_en": ["gte", "lte"]
    }
    search_fields = ["accion", "recurso", "recurso_id", "metadata"]
    ordering_fields = ["creado_en"]
    ordering = ["-creado_en"]
