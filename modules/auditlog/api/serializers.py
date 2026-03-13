from rest_framework import serializers
from modules.auditlog.models import AuditLog

class AuditLogSerializer(serializers.ModelSerializer):
    """
    Read-only serializer for activity logs.
    """
    usuario_nombre = serializers.CharField(source="usuario.nombre_completo", read_only=True)
    
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "usuario",
            "usuario_nombre",
            "accion",
            "recurso",
            "recurso_id",
            "metadata",
            "ip_address",
            "user_agent",
            "creado_en"
        ]
        read_only_fields = fields
