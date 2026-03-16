from rest_framework.throttling import SimpleRateThrottle
from django.core.cache import cache
from modules.billing.models import Suscripcion, EstadoSuscripcion
import logging

logger = logging.getLogger(__name__)

class TenantRateThrottle(SimpleRateThrottle):
    """
    Limits the request rate based on the Empresa's (Tenant) subscription plan.
    
    The rate is dynamically fetched from Plan.rate_limit_per_minute.
    Values are cached for 5 minutes to avoid DB overhead.
    """
    scope = 'tenant'

    def get_cache_key(self, request, view):
        if not hasattr(request, 'empresa') or not request.empresa:
            return None
            
        return self.cache_format % {
            'scope': self.scope,
            'ident': str(request.empresa.id)
        }

    def allow_request(self, request, view):
        """
        Override to set dynamic rate before evaluating.
        """
        if not hasattr(request, 'empresa') or not request.empresa:
            return True

        # Fetch rate for this specific tenant
        self.rate = self.get_rate_for_tenant(request.empresa)
        self.num_requests, self.duration = self.parse_rate(self.rate)

        return super().allow_request(request, view)

    def get_rate_for_tenant(self, empresa):
        """
        Returns the rate string (e.g. '100/min') based on the Empresa's Plan.
        """
        cache_key = f"throttle_rate_limit:{empresa.id}"
        rate = cache.get(cache_key)
        
        if rate is None:
            try:
                # Find the active subscription and its plan
                sub = Suscripcion.objects.filter(
                    empresa=empresa,
                    estado__in=[EstadoSuscripcion.ACTIVE, EstadoSuscripcion.TRIAL]
                ).select_related('plan').first()
                
                if sub and sub.plan.rate_limit_per_minute:
                    limit = sub.plan.rate_limit_per_minute
                    rate = f"{limit}/min"
                else:
                    # Default if no active plan or no limit defined
                    rate = "60/min"
                
                # Cache for performance
                cache.set(cache_key, rate, 300)
            except Exception as e:
                logger.error(f"Error resolving throttle rate for empresa {empresa.id}: {e}")
                rate = "60/min"
                
        return rate
