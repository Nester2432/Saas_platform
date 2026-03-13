from django.apps import AppConfig

class BillingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'modules.billing'

    def ready(self):
        import modules.billing.signals
