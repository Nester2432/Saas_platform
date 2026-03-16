"""
core/querysets/base.py

Base QuerySets for the SaaS platform.

Provides:
- SoftDeleteQuerySet: filters out soft-deleted records by default
- TenantQuerySet: adds .for_empresa() scoping
- SoftDeleteTenantQuerySet: combines both (used by most business models)

Important design note on with_deleted():
    The SoftDeleteManager's get_queryset() calls .alive() before returning,
    so the QuerySet methods operate on an already-filtered set.
    with_deleted() must NOT be called on the manager's default queryset —
    it is exposed correctly on the Manager level (returning a fresh
    unfiltered QuerySet). The method below is intentionally removed from
    the QuerySet to prevent the silent no-op bug where
    qs.with_deleted() would call self.all() on an already-filtered clone.
"""

from django.db import models
from django.utils import timezone
import auto_prefetch


class SoftDeleteQuerySet(auto_prefetch.QuerySet):
    """
    QuerySet that supports soft delete operations.

    Exposed via SoftDeleteManager which applies .alive() in get_queryset().

    Available methods once you have a queryset instance:
        qs.delete()       → soft deletes all matched records (sets deleted_at)
        qs.hard_delete()  → permanently removes from DB
        qs.alive()        → filters to non-deleted records
        qs.dead()         → filters to only deleted records

    NOTE: with_deleted() is intentionally NOT on the QuerySet.
    Use the manager: Model.objects.with_deleted() which returns a
    fresh QuerySet bypassing the alive() filter entirely.
    """

    def delete(self):
        """
        Soft delete: sets deleted_at=now() on all matched records.
        Does NOT remove rows from the database.
        Returns a tuple: (count, {model_label: count})
        """
        count = self.update(deleted_at=timezone.now())
        return count, {self.model._meta.label: count}

    def hard_delete(self):
        """Permanently delete all records in the queryset. Use with caution."""
        return super().delete()

    def alive(self):
        """Return only non-deleted (active) records."""
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        """Return only soft-deleted records."""
        return self.exclude(deleted_at__isnull=True)

class TenantQuerySet(auto_prefetch.QuerySet):
    """
    QuerySet that provides tenant (empresa) scoping.

    Usage:
        Model.objects.for_empresa(empresa_id)
        Model.objects.for_empresa(request.empresa)   # accepts instance or UUID
    """

    def for_empresa(self, empresa):
        """
        Filter by empresa. Accepts either an Empresa instance or a UUID.
        """
        if hasattr(empresa, 'pk'):
            return self.filter(empresa_id=empresa.pk)
        return self.filter(empresa_id=empresa)


class SoftDeleteTenantQuerySet(SoftDeleteQuerySet, TenantQuerySet):
    """
    Combined QuerySet for all business models.
    Provides: soft delete operations + tenant scoping.

    Do NOT call .with_deleted() on this queryset —
    use the manager: Model.objects.with_deleted().for_empresa(...)
    """
    pass
