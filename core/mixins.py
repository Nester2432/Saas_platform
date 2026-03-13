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
from modules.events.event_bus import EventBus

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
            if self.request.user.is_superuser:
                return qs
            raise PermissionDenied("Empresa no encontrada en el request.")

        # Use the custom manager's .for_empresa() method
        if hasattr(qs, "for_empresa"):
            return qs.for_empresa(empresa)

        # Fallback: filter directly (for models without EmpresaModel)
        return qs.filter(empresa=empresa)

    def perform_create(self, serializer):
        empresa = getattr(self.request, "empresa", None)
        if not empresa and not self.request.user.is_superuser:
            raise PermissionDenied("Empresa no encontrada en el request.")

        # If it's a superuser without an empresa, we allow save without it
        # (models like Suscripcion have empresa FK, so it might still fail if not provided in serializer)
        save_kwargs = {
            "created_by": self.request.user,
            "updated_by": self.request.user,
        }
        if empresa:
            save_kwargs["empresa"] = empresa
            
        serializer.save(**save_kwargs)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        """Soft delete instead of hard delete."""
        instance.soft_delete(user=self.request.user)
        # We also trigger AuditService manually here if needed, 
        # but the specific SoftDelete logic might be enough.
        logger.info(
            "Soft deleted %s id=%s by user=%s empresa=%s",
            instance.__class__.__name__,
            instance.id,
            self.request.user.id,
            getattr(self.request, "empresa_id", "None"),
        )


class AuditLogMixin:
    """
    Logs create/update/delete operations for audit trail.
    Add to ViewSets that manage sensitive data.
    Only logs updates if there are changed data.
    """

    def _get_audit_metadata(self, serializer):
        # In a real system, we'd sanitize sensitive fields here
        return serializer.validated_data

    def perform_create(self, serializer):
        super().perform_create(serializer)
        EventBus.publish(
            f"{serializer.instance.__class__.__name__.lower()}_creado",
            empresa_id=getattr(self.request, "empresa_id", None),
            usuario_id=self.request.user.id if self.request.user.is_authenticated else None,
            recurso=serializer.instance.__class__.__name__.lower(),
            recurso_id=serializer.instance.id,
            **self._get_audit_metadata(serializer)
        )

    def perform_update(self, serializer):
        # Optimization: Only log if there are real changes
        # We compare instance values with validated_data
        has_changes = False
        if serializer.instance:
            for attr, value in serializer.validated_data.items():
                if getattr(serializer.instance, attr) != value:
                    has_changes = True
                    break
        
        super().perform_update(serializer)
        
        if has_changes:
            EventBus.publish(
                f"{serializer.instance.__class__.__name__.lower()}_editado",
                empresa_id=getattr(self.request, "empresa_id", None),
                usuario_id=self.request.user.id if self.request.user.is_authenticated else None,
                recurso=serializer.instance.__class__.__name__.lower(),
                recurso_id=serializer.instance.id,
                **self._get_audit_metadata(serializer)
            )

    def perform_destroy(self, instance):
        EventBus.publish(
            f"{instance.__class__.__name__.lower()}_eliminado",
            empresa_id=getattr(self.request, "empresa_id", None),
            usuario_id=self.request.user.id if self.request.user.is_authenticated else None,
            recurso=instance.__class__.__name__.lower(),
            recurso_id=instance.id
        )
        super().perform_destroy(instance)
