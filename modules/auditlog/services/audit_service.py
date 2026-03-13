import logging
from django.utils import timezone
from modules.auditlog.models import AuditLog

logger = logging.getLogger(__name__)

class AuditService:
    """
    Service layer for managing the Audit Trail.
    Centralizes logging of business-critical actions.
    """

    @staticmethod
    def registrar_evento(
        empresa,
        usuario,
        accion,
        recurso,
        recurso_id=None,
        metadata=None,
        request=None
    ):
        """
        Creates an audit entry.
        Automatically extracts IP and User-Agent if request is provided.
        """
        ip_address = None
        user_agent = None

        if request:
            # Try to get IP (handles proxy/load balancer)
            path_info = request.META.get('HTTP_X_FORWARDED_FOR')
            if path_info:
                ip_address = path_info.split(',')[0]
            else:
                ip_address = request.META.get('REMOTE_ADDR')
            
            user_agent = request.META.get('HTTP_USER_AGENT')

        try:
            return AuditLog.objects.create(
                empresa=empresa,
                usuario=usuario,
                accion=accion,
                recurso=recurso,
                recurso_id=str(recurso_id) if recurso_id else None,
                metadata=metadata,
                ip_address=ip_address,
                user_agent=user_agent
            )
        except Exception as e:
            # We never want to break the main transaction because auditing failed
            logger.error(f"Error al registrar log de auditoría: {str(e)}")
            return None
