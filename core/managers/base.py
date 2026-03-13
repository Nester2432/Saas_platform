"""
core/managers/base.py

Custom Django managers that wire up the custom QuerySets.

The manager is the correct place to expose with_deleted() because it
constructs a FRESH, unfiltered QuerySet — bypassing the alive() call
in get_queryset(). Calling .with_deleted() on an already-filtered
QuerySet would be a no-op since alive() already excluded the rows.

Correct usage pattern:
    Model.objects.all()                          → active records only
    Model.objects.with_deleted()                 → all records
    Model.objects.deleted_only()                 → only deleted
    Model.objects.for_empresa(e)                 → active, scoped to tenant
    Model.objects.with_deleted().for_empresa(e)  → all, scoped to tenant
"""

from django.db import models
from core.querysets.base import (
    SoftDeleteQuerySet,
    TenantQuerySet,
    SoftDeleteTenantQuerySet,
)


class SoftDeleteManager(models.Manager):
    """
    Manager for models with soft delete only (no tenant scoping).
    Used for non-tenant models like Modulo, Rol, etc.

    Default queryset excludes soft-deleted records.
    Use .with_deleted() to include them.
    """

    def get_queryset(self):
        return SoftDeleteQuerySet(self.model, using=self._db).alive()

    def with_deleted(self):
        return SoftDeleteQuerySet(self.model, using=self._db)

    def deleted_only(self):
        return SoftDeleteQuerySet(self.model, using=self._db).dead()


class TenantManager(models.Manager):
    """
    Manager for models with tenant scoping only (no soft delete).
    Rare — prefer SoftDeleteTenantManager for business models.
    """

    def get_queryset(self):
        from core.utils.tenant_context import get_current_empresa_id
        qs = TenantQuerySet(self.model, using=self._db)
        
        # Automatic filtering if context is active
        empresa_id = get_current_empresa_id()
        if empresa_id:
            qs = qs.for_empresa(empresa_id)
            
        return qs

    def for_empresa(self, empresa):
        """
        Explicitly filter by empresa, ignoring the global context if set.
        """
        return TenantQuerySet(self.model, using=self._db).for_empresa(empresa)


class SoftDeleteTenantManager(models.Manager):
    """
    Standard manager for all business models.

    Combines:
    - Soft delete (excludes deleted_at records by default)
    - Tenant scoping (.for_empresa())

    Usage:
        Cliente.objects.for_empresa(request.empresa)
        Cliente.objects.with_deleted().for_empresa(empresa)
        Cliente.objects.deleted_only()
    """

    def get_queryset(self):
        from core.utils.tenant_context import get_current_empresa
        
        qs = SoftDeleteTenantQuerySet(self.model, using=self._db).alive()
        
        # Automatic filtering if context is active
        empresa_id = get_current_empresa()
        if empresa_id:
            qs = qs.for_empresa(empresa_id)
            
        return qs

    def with_deleted(self):
        from core.utils.tenant_context import get_current_empresa
        
        qs = SoftDeleteTenantQuerySet(self.model, using=self._db)
        
        # Automatic filtering if context is active
        empresa_id = get_current_empresa()
        if empresa_id:
            qs = qs.for_empresa(empresa_id)
            
        return qs

    def deleted_only(self):
        from core.utils.tenant_context import get_current_empresa
        
        qs = SoftDeleteTenantQuerySet(self.model, using=self._db).dead()
        
        # Automatic filtering if context is active
        empresa_id = get_current_empresa()
        if empresa_id:
            qs = qs.for_empresa(empresa_id)
            
        return qs

    def for_empresa(self, empresa):
        """
        Explicitly filter by empresa, ignoring the global context if set.
        Useful for internal tasks or cross-tenant operations.
        """
        return SoftDeleteTenantQuerySet(self.model, using=self._db).alive().for_empresa(empresa)
