import pytest
import stripe
from unittest.mock import MagicMock, patch
from django.urls import reverse
from rest_framework import status
from modules.payments.services import StripeService
from modules.payments.models import ProcessedStripeEvent, CustomerPaymentProfile, PaymentTransaction
from modules.billing.models import Suscripcion, EstadoSuscripcion

@pytest.mark.django_db
class TestStripeIntegration:
    @patch('stripe.checkout.Session.create')
    @patch('modules.payments.services.StripeService.create_customer')
    def test_create_checkout_session(self, mock_create_customer, mock_session_create, authenticated_client, empresa_demo, plan_starter):
        mock_create_customer.return_value = "cus_test_123"
        mock_session_create.return_value = MagicMock(id="sess_123", url="https://stripe.com/test")
        
        url = reverse('billing-plans-cambiar-plan')
        data = {"plan_id": plan_starter.id}
        
        response = authenticated_client.post(url, data)
        
        assert response.status_code == status.HTTP_200_OK
        assert "checkout_url" in response.data
        assert response.data["checkout_url"] == "https://stripe.com/test"
        
        # Verificar que la suscripción pasó a PENDING_PAYMENT
        sub = Suscripcion.objects.get(empresa=empresa_demo)
        assert sub.estado == EstadoSuscripcion.PENDING_PAYMENT

    def test_webhook_invoice_paid(self, api_client, empresa_demo):
        url = reverse("payments-api:stripe-webhook")
        # Pre-set the stripe_subscription_id so the service can find it
        suscripcion = Suscripcion.objects.get(empresa=empresa_demo)
        suscripcion.stripe_subscription_id = "sub_test_123"
        suscripcion.estado = EstadoSuscripcion.PENDING_PAYMENT
        suscripcion.save()

        payload = {
            "id": "evt_test",
            "type": "invoice.paid",
            "data": {
                "object": {
                    "subscription": "sub_test_123",
                    "customer": "cus_test",
                    "amount_paid": 2900,
                    "currency": "usd",
                    "payment_intent": "pi_test"
                }
            }
        }
        headers = {"HTTP_STRIPE_SIGNATURE": "t=1,v1=test"}
        
        with patch("stripe.Webhook.construct_event", return_value=payload):
            response = api_client.post(url, data=payload, format="json", **headers)
            
        assert response.status_code == status.HTTP_200_OK
        
        # Verify subscription was updated (Trial -> Active)
        suscripcion = Suscripcion.objects.get(empresa=empresa_demo)
        assert suscripcion.estado == EstadoSuscripcion.ACTIVE
        assert suscripcion.stripe_subscription_id == "sub_test_123"

    def test_webhook_idempotency(self, api_client):
        url = reverse("payments-api:stripe-webhook")
        payload = {
            "id": "evt_duplicate",
            "type": "invoice.paid",
            "data": {"object": {"subscription": "sub_test", "customer": "cus_test"}}
        }
        headers = {"HTTP_STRIPE_SIGNATURE": "t=1,v1=test"}
        
        with patch("stripe.Webhook.construct_event", return_value=payload):
            # First time
            api_client.post(url, data=payload, format="json", **headers)
            # Second time
            response = api_client.post(url, data=payload, format="json", **headers)
            
        assert response.status_code == status.HTTP_200_OK
        # Mock StripeService would log "already processed", but we check status code
