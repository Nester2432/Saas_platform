from django.apps import AppConfig

class AuditLogConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "modules.auditlog"
    verbose_name = "Registro de Auditoría"

    def ready(self):
        import modules.auditlog.signals
