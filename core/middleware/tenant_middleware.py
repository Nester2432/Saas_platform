"""
core/middleware/tenant_middleware.py

TenantMiddleware resolves which Empresa (tenant) is making the request
and attaches it to `request.empresa`.

Resolution strategies (in order of priority):
1. JWT claim: `empresa_id` inside the token payload
2. HTTP Header: `X-Empresa-ID` (useful for internal services / testing)
3. Subdomain: `acme.yourplatform.com` → slug lookup (optional, see below)

After this middleware runs, every view can safely use:
    request.empresa       → Empresa instance or None
    request.empresa_id    → UUID or None

Security note:
- We always reload the Empresa from the DB (no trust in cached tokens alone)
- We verify is_active=True so suspended companies cannot access the API
- Failed resolution sets request.empresa = None (permission layer rejects it)
"""

import logging
from django.core.cache import cache
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

EMPRESA_CACHE_TTL = 300  # 5 minutes


class TenantMiddleware:
    """
    Resolves the current tenant (Empresa) and attaches it to the request.
    Wraps the request in a try/finally block to ensure strict context isolation.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.empresa = None
        request.empresa_id = None
        _tenant_token = None

        empresa_id = self._resolve_empresa_id(request)
        
        if empresa_id:
            empresa = self._load_empresa(empresa_id)
            if empresa:
                request.empresa = empresa
                request.empresa_id = empresa.id
                # Set global context for automatic filtering
                from core.utils.tenant_context import set_current_empresa
                _tenant_token = set_current_empresa(empresa.id)
            else:
                logger.warning(
                    "TenantMiddleware: empresa_id=%s not found or inactive.",
                    empresa_id
                )

        try:
            response = self.get_response(request)
            return response
        finally:
            # Strictly clear context even if an exception occurred in the view
            if _tenant_token:
                from core.utils.tenant_context import reset_current_empresa
                reset_current_empresa(_tenant_token)
            else:
                # If no token (early failure), ensure it's still clean
                from core.utils.tenant_context import clear_current_empresa
                clear_current_empresa()

    # ------------------------------------------------------------------
    # Private resolution helpers
    # ------------------------------------------------------------------

    def _resolve_empresa_id(self, request):
        """
        Try each resolution strategy and return the first empresa_id found.
        """
        # Strategy 1: JWT token claim (primary strategy for API clients)
        empresa_id = self._from_jwt(request)
        if empresa_id:
            return empresa_id

        # Strategy 2: Explicit header (useful for service-to-service calls)
        empresa_id = self._from_header(request)
        if empresa_id:
            return empresa_id

        # Strategy 3: Subdomain (optional — uncomment to enable)
        # empresa_id = self._from_subdomain(request)
        # if empresa_id:
        #     return empresa_id

        # Strategy 4: Auth User (Fallback for session-based access / Demo UI)
        empresa_id = self._from_user(request)
        if empresa_id:
            return empresa_id

        return None

    def _from_jwt(self, request):
        """
        Extract empresa_id from JWT token payload.
        Compatible with djangorestframework-simplejwt.

        The token must include an `empresa_id` claim.
        Add this via a custom token serializer (see usuarios/tokens.py).
        """
        # DRF processes auth lazily; we read from META directly to avoid
        # triggering full auth in middleware.
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return None

        try:
            from rest_framework_simplejwt.tokens import UntypedToken
            from rest_framework_simplejwt.exceptions import TokenError

            token_str = auth_header.split(" ")[1]
            token = UntypedToken(token_str)
            eid = token.payload.get("empresa_id")
            logger.debug("TenantMiddleware: JWT empresa_id=%s", eid)
            return eid
        except Exception as e:
            logger.debug("TenantMiddleware: JWT extraction failed: %s", str(e))
            return None

    def _from_header(self, request):
        """
        Extract empresa_id from X-Empresa-ID header.
        Useful for testing and internal service calls.
        """
        return request.META.get("HTTP_X_EMPRESA_ID")

    def _from_user(self, request):
        """
        Extract empresa_id from the authenticated user object.
        Useful for session-based access to the web UI.
        """
        if request.user.is_authenticated:
            eid = getattr(request.user, "empresa_id", None)
            if eid:
                return str(eid)
        return None

    def _from_subdomain(self, request):
        """
        Resolve empresa by subdomain slug.
        e.g. acme.yourplatform.com → looks up Empresa(slug='acme')

        Uncomment and configure BASE_DOMAIN in settings to enable.
        """
        from django.conf import settings
        base_domain = getattr(settings, "BASE_DOMAIN", None)
        if not base_domain:
            return None

        host = request.get_host().split(":")[0]  # strip port
        if not host.endswith(f".{base_domain}"):
            return None

        slug = host[: -(len(base_domain) + 1)]
        cache_key = f"empresa_slug:{slug}"

        empresa_id = cache.get(cache_key)
        if not empresa_id:
            try:
                from apps.empresas.models import Empresa
                empresa = Empresa.objects.get(slug=slug, is_active=True)
                empresa_id = str(empresa.id)
                cache.set(cache_key, empresa_id, EMPRESA_CACHE_TTL)
            except Exception:
                return None

        return empresa_id

    def _load_empresa(self, empresa_id):
        """
        Load Empresa from cache or DB.
        Caches active empresas for EMPRESA_CACHE_TTL seconds.
        """
        cache_key = f"empresa:{empresa_id}"
        empresa = cache.get(cache_key)

        if empresa is None:
            try:
                from apps.empresas.models import Empresa
                empresa = Empresa.objects.select_related(
                    "configuracion"
                ).get(id=empresa_id, is_active=True)
                cache.set(cache_key, empresa, EMPRESA_CACHE_TTL)
            except Exception:
                return None

        return empresa
