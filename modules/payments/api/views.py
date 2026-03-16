import stripe
import logging
from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from modules.payments.services import StripeService

logger = logging.getLogger(__name__)

class StripeWebhookView(APIView):
    """
    Endpoint para recibir webhooks de Stripe.
    Verifica la firma y procesa el evento asíncronamente (o síncronamente con idempotencia).
    """
    permission_classes = [] # Public access
    
    @method_decorator(csrf_exempt)
    def post(self, request, *args, **kwargs):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
        endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, endpoint_secret
            )
        except ValueError as e:
            # Invalid payload
            return Response(status=status.HTTP_400_BAD_REQUEST)
        except stripe.error.SignatureVerificationError as e:
            # Invalid signature
            return Response(status=status.HTTP_400_BAD_REQUEST)

        # Procesar el evento
        try:
            StripeService.handle_webhook(event)
        except Exception as e:
            logger.error(f"Error processing Stripe webhook: {e}")
            return Response({"error": "Webhook processing failed"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"status": "success"}, status=status.HTTP_200_OK)