"""
apps/usuarios/tokens.py  —  DEPRECATED

This module has been moved to:
    apps/usuarios/auth/serializers.py

This shim re-exports everything from the new location so any existing
imports continue to work. Remove after confirming all references are updated.
"""

# Re-export from canonical location
from apps.usuarios.auth.serializers import (  # noqa: F401
    CustomTokenObtainPairSerializer,
    get_tokens_for_user,
)
