import uuid
from django.db import models
from core.models import EmpresaModel

class EventStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    PROCESSED = "PROCESSED", "Processed"
    FAILED = "FAILED", "Failed"
    FAILED_PERMANENT = "FAILED_PERMANENT", "Failed Permanent"

class EventStore(EmpresaModel):
    """
    Persistent store for all events published through the EventBus.
    Provides traceability, reliability, and replay capabilities.
    
    Inherits from EmpresaModel:
    - id (UUID PK)
    - empresa (ForeignKey to apps.empresas.Empresa)
    - created_at, updated_at
    - created_by, updated_by
    - deleted_at (soft delete)
    """
    event_name = models.CharField(max_length=100)
    event_id = models.UUIDField(unique=True, help_text="Unique identifier from the Event object")
    version = models.CharField(max_length=10, default="1.0", help_text="Event schema version")
    
    # We keep usuario_id as CharField for external/legacy compatibility if needed, 
    # but EmpresaModel already provides created_by/updated_by if we want to use them.
    usuario_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    
    payload = models.JSONField()
    
    status = models.CharField(
        max_length=20, 
        choices=EventStatus.choices, 
        default=EventStatus.PENDING,
        db_index=True
    )
    
    retry_count = models.IntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)
    
    error_log = models.TextField(null=True, blank=True, help_text="Last error message if failed")

    class Meta:
        verbose_name = "Event Store"
        verbose_name_plural = "Event Store"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["event_name"]),
            models.Index(fields=["created_at"]),
            models.Index(
                fields=["empresa", "status", "-created_at"],
                name="event_emp_stat_created_idx"
            ),
        ]

    def __str__(self):
        return f"{self.event_name} ({self.event_id}) - {self.status}"
