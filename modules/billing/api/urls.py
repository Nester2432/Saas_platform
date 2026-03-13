from django.urls import path, include
from rest_framework.routers import DefaultRouter
from modules.billing.api.views import PlanViewSet, BillingViewSet

router = DefaultRouter()
router.register(r"plans", PlanViewSet, basename="billing-plans")
router.register(r"config", BillingViewSet, basename="billing-config")

urlpatterns = [
    path("", include(router.urls)),
]
