"""
core/models.py

Abstract base models for the SaaS platform.

Model hierarchy:
    BaseModel          → UUID PK, timestamps, soft delete, audit fields
    EmpresaModel       → BaseModel + empresa FK (multi-tenant)

All business models MUST inherit from EmpresaModel.
Non-tenant platform models (Empresa, Modulo, etc.) inherit from BaseModel.

Design decisions:
- UUID PKs: avoid exposing sequential IDs, safer for multi-tenant APIs
- Soft delete: never lose data, enable audit trails and recovery
- created_by/updated_by: full audit trail without external audit lib
- EmpresaModel enforces tenant FK at the ORM level (not just convention)
"""

import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone
import auto_prefetch

from core.managers.base import SoftDeleteManager, SoftDeleteTenantManager


class BaseModel(models.Model):
    """
    Abstract base for ALL models in the platform.

    Provides:
    - UUID primary key (public-safe, no sequential ID leakage)
    - Automatic timestamps (created_at, updated_at)
    - Soft delete (deleted_at + soft_delete() method)
    - Audit trail (created_by, updated_by)

    Manager:
        .objects          → excludes soft-deleted records
        .objects.with_deleted() → includes all
        .objects.deleted_only() → only deleted
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Soft delete timestamp. NULL means record is active."
    )
    created_by = auto_prefetch.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_created",
        help_text="User who created this record."
    )
    updated_by = auto_prefetch.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="%(app_label)s_%(class)s_updated",
        help_text="User who last updated this record."
    )

    objects = SoftDeleteManager()

    class Meta:
        abstract = True

    def soft_delete(self, user=None):
        """
        Soft delete: marks deleted_at, does not remove from DB.
        Pass user to record who deleted it.
        """
        self.deleted_at = timezone.now()
        if user:
            self.updated_by = user
        self.save(update_fields=["deleted_at", "updated_by"])

    def restore(self):
        """Restore a soft-deleted record."""
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])

    def hard_delete(self):
        """Permanently delete. Use with caution."""
        super().delete()

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def __repr__(self):
        return f"<{self.__class__.__name__} id={self.id}>"


class EmpresaModel(BaseModel):
    """
    Abstract base for ALL multi-tenant business models.

    Inherits BaseModel and adds:
    - empresa FK (mandatory tenant scope)
    - DB index on empresa for fast filtering
    - .objects.for_empresa(empresa) via SoftDeleteTenantManager

    Every model representing a business entity MUST inherit from this.
    This makes it impossible to accidentally forget the tenant FK.

    Example:
        class Cliente(EmpresaModel):
            nombre = models.CharField(max_length=200)
            # empresa is inherited — no need to declare it

        # Usage:
        Cliente.objects.for_empresa(request.empresa)
    """

    empresa = auto_prefetch.ForeignKey(
        "empresas.Empresa",
        on_delete=models.CASCADE,
        related_name="%(app_label)s_%(class)s_set",
        db_index=True,
    )

    objects = SoftDeleteTenantManager()

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=["empresa", "created_at"]),
            models.Index(fields=["empresa", "deleted_at"]),
        ]
