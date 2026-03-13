from django.http import JsonResponse
from django.urls import resolve
from django.core.exceptions import ValidationError
from modules.billing.services.billing_service import BillingService

class SubscriptionGuardMiddleware:
    """
    Middleware that blocks write operations (POST, PUT, PATCH, DELETE)
    if the company's subscription is not ACTIVE.
    
    Exemptions:
    - Safe methods (GET, HEAD, OPTIONS).
    - Authentication endpoints.
    - Subscription status and plans endpoints.
    - System health check.
    """
    
    # Whitelisted URL names or paths
    WHITELISTED_PATHS = [
        "/api/v1/auth/",
        "/api/v1/health/",
        "/api/v1/billing/suscripcion/actual/",
        "/api/v1/billing/planes/",
        "/api/v1/payments/webhook",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. Allow safe methods automatically
        if request.method in ["GET", "HEAD", "OPTIONS"]:
            return self.get_response(request)

        # 2. Check whitelist
        path = request.path
        if any(path.startswith(wp) for wp in self.WHITELISTED_PATHS):
            return self.get_response(request)

        # 3. Handle operations on tenants
        # request.empresa is populated by TenantMiddleware
        empresa = getattr(request, "empresa", None)
        
        if empresa:
            try:
                BillingService.verificar_suscripcion_activa(empresa)
            except ValidationError as e:
                return JsonResponse(
                    {
                        "error": "Suscripción Inactiva",
                        "detail": str(e.message) if hasattr(e, "message") else str(e)
                    },
                    status=403
                )

        return self.get_response(request)
