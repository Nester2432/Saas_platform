"""
config/settings/production.py

Production settings for Render deployment.
All values are configurable via environment variables.
"""
from .base import *
import dj_database_url
import os

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "False") == "True"

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-change-me-in-production")

# ---------------------------------------------------------------------------
# Hosts — Render + custom domain support
# ---------------------------------------------------------------------------

_ALLOWED_HOSTS_ENV = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _ALLOWED_HOSTS_ENV.split(",") if h.strip()] + [
    "localhost",
    "127.0.0.1",
    ".onrender.com",  # Wildcard for all Render subdomains
]

# Required when behind Render's load balancer
CSRF_TRUSTED_ORIGINS = [
    f"https://{h}" for h in ALLOWED_HOSTS if "." in h and not h.startswith(".")
] + ["https://*.onrender.com"]

# ---------------------------------------------------------------------------
# Database — PostgreSQL via DATABASE_URL, SQLite fallback for demos
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
else:
    # Fallback to SQLite for demo deploys without PostgreSQL
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ---------------------------------------------------------------------------
# Cache — Redis fallback to LocMemCache for demo deploys
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "")

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": 300,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
            },
        }
    }
else:
    # Fallback to in-memory cache (no Redis needed for demo)
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

# ---------------------------------------------------------------------------
# Static Files — WhiteNoise
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Insert WhiteNoise after SecurityMiddleware
_whitenoise = "whitenoise.middleware.WhiteNoiseMiddleware"
if _whitenoise not in MIDDLEWARE:
    MIDDLEWARE.insert(1, _whitenoise)

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# Only redirect to HTTPS if explicitly enabled (disable for HTTP-only Render demo)
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "False") == "True"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

CORS_ALLOW_ALL_ORIGINS = True  # Demo mode — restrict in production

# ---------------------------------------------------------------------------
# Celery (optional, gracefully degraded if no broker)
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", REDIS_URL or "memory://")
