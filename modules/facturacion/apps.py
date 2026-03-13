from django.apps import AppConfig

class FacturacionConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'modules.facturacion'

    def ready(self):
        import modules.facturacion.signals
