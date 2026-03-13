from django.urls import path
from .views import StripeWebhookView

app_name = "payments-api"

urlpatterns = [
    path("webhook", StripeWebhookView.as_view(), name="stripe-webhook"),
]
