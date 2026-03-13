from django.apps import AppConfig

class EventsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'modules.events'

    def ready(self):
        # Register handlers here to avoid circular imports and ensure they are ready
        from . import handlers
