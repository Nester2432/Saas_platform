from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver
from modules.events.event_bus import EventBus

@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    """
    Publish login event.
    """
    EventBus.publish(
        "login",
        empresa_id=user.empresa_id,
        usuario_id=user.id,
        recurso="usuario",
        recurso_id=user.id
    )

@receiver(user_logged_out)
def log_user_logout(sender, request, user, **kwargs):
    """
    Publish logout event.
    """
    if user:
        EventBus.publish(
            "logout",
            empresa_id=user.empresa_id,
            usuario_id=user.id,
            recurso="usuario",
            recurso_id=user.id
        )
