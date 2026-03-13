from django.db import models
import uuid

class AuditLog(models.Model):
    """
    Centralized activity log for the multi-tenant SaaS platform.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Context (Indexed for performance)
    empresa = models.ForeignKey(
        "empresas.Empresa", 
        on_delete=models.CASCADE, 
        related_name="audit_logs",
        db_index=True
    )
    usuario = models.ForeignKey(
        "usuarios.Usuario", 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name="audit_logs",
        db_index=True
    )
    
    # Event data (Indexed for filtering)
    accion = models.CharField(max_length=100, db_index=True)  # e.g., 'crear_venta'
    recurso = models.CharField(max_length=50)                 # e.g., 'venta'
    recurso_id = models.CharField(max_length=64, null=True, blank=True)
    
    # Payload
    metadata = models.JSONField(null=True, blank=True)
    
    # Request data
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    
    # Timeline
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-creado_en"]
        indexes = [
            # Composite index for tenant-based history queries
            models.Index(fields=["empresa", "creado_en"], name="idx_audit_empresa_date"),
        ]
        verbose_name = "Log de Auditoría"
        verbose_name_plural = "Logs de Auditoría"

    def __str__(self):
        return f"{self.accion} - {self.recurso} ({self.creado_en})"
