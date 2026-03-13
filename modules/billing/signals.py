import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.empresas.models import Empresa
from modules.billing.models import Suscripcion, Plan, EstadoSuscripcion
from modules.billing.services.billing_service import BillingService

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Empresa)
def create_trial_subscription(sender, instance, created, **kwargs):
    """
    Automatically creates a 14-day TRIAL subscription when a new Empresa is created.
    """
    if created:
        try:
            # Get default free plan (assuming 'free' slug or lowest price)
            plan = Plan.objects.filter(activo=True).order_by('precio_mensual').first()
            if not plan:
                logger.error("No active plans found to create trial subscription.")
                return

            # Check if subscription already exists (idempotency)
            if not Suscripcion.objects.filter(empresa=instance).exists():
                BillingService.create_subscription(
                    empresa=instance,
                    plan=plan,
                    is_trial=True
                )
                logger.info(f"Trial subscription created for empresa {instance.nombre}")
        except Exception as e:
            logger.error(f"Error creating trial subscription for {instance.nombre}: {str(e)}")
