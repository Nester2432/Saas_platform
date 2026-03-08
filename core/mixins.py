"""
core/mixins.py

ViewSet mixins that enforce multi-tenant safety and reduce boilerplate.

TenantQuerysetMixin:
    - Automatically scopes all querysets to request.empresa
    - Automatically sets empresa, created_by, updated_by on save

Usage:
    class ClienteViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
        queryset = Cliente.objects.all()  # will be auto-scoped
        serializer_class = ClienteSerializer
        modulo_requerido = "clientes"
"""

import logging
from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)


class TenantQuerysetMixin:
    """
    Mixin for ModelViewSet that:
    1. Scopes queryset to request.empresa automatically
    2. Injects empresa into created objects
    3. Sets created_by / updated_by from request.user
    4. Prevents cross-tenant access on object retrieval

    Place BEFORE ModelViewSet in MRO:
        class ClienteViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """

    def get_queryset(self):
        qs = super().get_queryset()
        empresa = getattr(self.request, "empresa", None)

        if not empresa:
            raise PermissionDenied("Empresa no encontrada en el request.")

        # Use the custom manager's .for_empresa() method
        if hasattr(qs, "for_empresa"):
            return qs.for_empresa(empresa)

        # Fallback: filter directly (for models without EmpresaModel)
        return qs.filter(empresa=empresa)

    def perform_create(self, serializer):
        empresa = getattr(self.request, "empresa", None)
        if not empresa:
            raise PermissionDenied("Empresa no encontrada en el request.")

        serializer.save(
            empresa=empresa,
            created_by=self.request.user,
            updated_by=self.request.user,
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        """Soft delete instead of hard delete."""
        instance.soft_delete(user=self.request.user)
        logger.info(
            "Soft deleted %s id=%s by user=%s empresa=%s",
            instance.__class__.__name__,
            instance.id,
            self.request.user.id,
            self.request.empresa_id,
        )


class AuditLogMixin:
    """
    Logs create/update/delete operations for audit trail.
    Add to ViewSets that manage sensitive data.
    """

    def perform_create(self, serializer):
        super().perform_create(serializer)
        logger.info(
            "CREATED %s id=%s by user=%s empresa=%s",
            serializer.instance.__class__.__name__,
            serializer.instance.id,
            self.request.user.id,
            getattr(self.request, "empresa_id", None),
        )

    def perform_update(self, serializer):
        super().perform_update(serializer)
        logger.info(
            "UPDATED %s id=%s by user=%s empresa=%s",
            serializer.instance.__class__.__name__,
            serializer.instance.id,
            self.request.user.id,
            getattr(self.request, "empresa_id", None),
        )

    def perform_destroy(self, instance):
        logger.info(
            "DELETED %s id=%s by user=%s empresa=%s",
            instance.__class__.__name__,
            instance.id,
            self.request.user.id,
            getattr(self.request, "empresa_id", None),
        )
        super().perform_destroy(instance)
