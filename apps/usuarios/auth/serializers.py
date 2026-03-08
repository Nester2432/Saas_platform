"""
apps/usuarios/auth/serializers.py

JWT authentication serializers.

CustomTokenObtainPairSerializer enriches the token payload with
tenant and role data that the rest of the platform depends on:

    token["empresa_id"]        → read by TenantMiddleware on every request
    token["is_empresa_admin"]  → read by IsEmpresaAdmin permission
    token["is_platform_admin"] → read by platform-level views

Configure in settings.py:
    SIMPLE_JWT = {
        "TOKEN_OBTAIN_SERIALIZER": "apps.usuarios.auth.serializers.CustomTokenObtainPairSerializer",
    }
"""

from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Extends the default simplejwt serializer to embed tenant context
    and user metadata directly into the JWT payload.

    Token payload claims added:
        nombre          → user's full name (display only)
        email           → user's email (display only)
        empresa_id      → UUID of the user's empresa (used by TenantMiddleware)
        is_empresa_admin  → bool (used by IsEmpresaAdmin permission)
        is_platform_admin → bool (used by platform admin views)

    Login response body:
        {
            "access":  "<jwt>",
            "refresh": "<jwt>",
            "usuario": {
                "id": "...",
                "nombre": "...",
                "email": "...",
                "empresa_id": "...",
                "is_empresa_admin": false
            }
        }
    """

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Identity claims (informational — not used for auth decisions)
        token["nombre"] = user.nombre_completo
        token["email"] = user.email

        # Tenant claim — the critical one, consumed by TenantMiddleware
        token["empresa_id"] = str(user.empresa_id) if user.empresa_id else None

        # Access level claims
        token["is_empresa_admin"] = user.is_empresa_admin
        token["is_platform_admin"] = user.is_platform_admin

        return token

    def validate(self, attrs):
        data = super().validate(attrs)

        # Attach user summary to the login response
        # Frontend uses this to initialise the session without an extra /me call
        data["usuario"] = {
            "id": str(self.user.id),
            "nombre": self.user.nombre_completo,
            "email": self.user.email,
            "empresa_id": str(self.user.empresa_id) if self.user.empresa_id else None,
            "is_empresa_admin": self.user.is_empresa_admin,
        }
        return data


def get_tokens_for_user(user):
    """
    Programmatically generate a token pair for a user instance.

    Used in:
    - Tests (create authenticated test clients without HTTP login)
    - Custom auth flows (e.g. social auth callback)
    - Management commands that need to impersonate a user

    Example:
        tokens = get_tokens_for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    """
    refresh = RefreshToken.for_user(user)

    # Embed the same claims as CustomTokenObtainPairSerializer
    refresh["empresa_id"] = str(user.empresa_id) if user.empresa_id else None
    refresh["is_empresa_admin"] = user.is_empresa_admin
    refresh["is_platform_admin"] = user.is_platform_admin
    refresh["nombre"] = user.nombre_completo
    refresh["email"] = user.email

    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }
