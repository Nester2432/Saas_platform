import stripe
import logging
from django.conf import settings
from django.db import transaction
from .models import CustomerPaymentProfile, PaymentTransaction, PaymentInvoice, ProcessedStripeEvent
from modules.billing.models import Suscripcion, EstadoSuscripcion

logger = logging.getLogger(__name__)
stripe.api_key = settings.STRIPE_SECRET_KEY

class StripeService:
    @staticmethod
    def create_customer(empresa):
        """
        Crea un cliente en Stripe y guarda el ID en el perfil local.
        """
        profile, created = CustomerPaymentProfile.objects.get_or_create(empresa=empresa)
        if not created and profile.stripe_customer_id:
            return profile.stripe_customer_id

        try:
            customer = stripe.Customer.create(
                email=empresa.email,
                name=empresa.nombre,
                metadata={
                    "empresa_id": str(empresa.id)
                }
            )
            profile.stripe_customer_id = customer.id
            profile.save()
            return customer.id
        except stripe.error.StripeError as e:
            logger.error(f"Error creating Stripe customer: {e}")
            raise

    @staticmethod
    def create_checkout_session(empresa, plan, success_url, cancel_url):
        """
        Crea una sesión de Stripe Checkout para suscripción.
        """
        customer_id = StripeService.create_customer(empresa)
        
        # En una implementación real, buscaríamos el price_id de Stripe asociado al plan.
        # Por ahora usaremos metadata o asumiremos que el plan tiene un stripe_price_id.
        price_id = getattr(plan, 'stripe_price_id', 'price_placeholder') # Placeholder

        try:
            session = stripe.checkout.Session.create(
                customer=customer_id,
                payment_method_types=['card'],
                line_items=[{
                    'price': price_id,
                    'quantity': 1,
                }],
                mode='subscription',
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={
                    "empresa_id": str(empresa.id),
                    "plan_id": str(plan.id)
                }
            )
            return session
        except stripe.error.StripeError as e:
            logger.error(f"Error creating Stripe checkout session: {e}")
            raise

    @staticmethod
    def cancel_subscription(stripe_subscription_id):
        """
        Cancela una suscripción en Stripe.
        """
        try:
            stripe.Subscription.delete(stripe_subscription_id)
            return True
        except stripe.error.StripeError as e:
            logger.error(f"Error cancelling Stripe subscription: {e}")
            return False

    @staticmethod
    def handle_webhook(event):
        """
        Procesa eventos de Stripe de forma idempotente.
        """
        event_id = event['id']
        if ProcessedStripeEvent.objects.filter(stripe_event_id=event_id).exists():
            logger.info(f"Stripe event {event_id} already processed.")
            return

        with transaction.atomic():
            event_type = event['type']
            data = event['data']['object']
            
            if event_type == 'invoice.paid':
                StripeService._handle_invoice_paid(data)
            elif event_type == 'invoice.payment_failed':
                StripeService._handle_invoice_payment_failed(data)
            elif event_type == 'customer.subscription.deleted':
                StripeService._handle_subscription_deleted(data)
            
            ProcessedStripeEvent.objects.create(stripe_event_id=event_id)

    @staticmethod
    def _handle_invoice_paid(invoice_data):
        subscription_id = invoice_data.get('subscription')
        customer_id = invoice_data.get('customer')
        
        try:
            sub = Suscripcion.objects.get(stripe_subscription_id=subscription_id)
            sub.estado = EstadoSuscripcion.ACTIVE
            # Actualizar fecha_fin si fuera necesario basado en el periodo de Stripe
            sub.save(update_fields=['estado'])
            
            PaymentTransaction.objects.create(
                empresa=sub.empresa,
                amount=invoice_data['amount_paid'] / 100,
                currency=invoice_data['currency'],
                status='paid',
                stripe_payment_intent_id=invoice_data.get('payment_intent')
            )
        except Suscripcion.DoesNotExist:
            logger.error(f"Subscription {subscription_id} not found locally.")

    @staticmethod
    def _handle_invoice_payment_failed(invoice_data):
        subscription_id = invoice_data.get('subscription')
        try:
            sub = Suscripcion.objects.get(stripe_subscription_id=subscription_id)
            sub.estado = EstadoSuscripcion.PAST_DUE
            sub.save(update_fields=['estado'])
            
            PaymentTransaction.objects.create(
                empresa=sub.empresa,
                amount=invoice_data['amount_due'] / 100,
                currency=invoice_data['currency'],
                status='failed',
                stripe_payment_intent_id=invoice_data.get('payment_intent')
            )
        except Suscripcion.DoesNotExist:
            logger.error(f"Subscription {subscription_id} not found locally.")

    @staticmethod
    def _handle_subscription_deleted(subscription_data):
        subscription_id = subscription_data['id']
        try:
            sub = Suscripcion.objects.get(stripe_subscription_id=subscription_id)
            sub.estado = EstadoSuscripcion.CANCELED
            sub.save(update_fields=['estado'])
        except Suscripcion.DoesNotExist:
            logger.error(f"Subscription {subscription_id} not found locally.")
