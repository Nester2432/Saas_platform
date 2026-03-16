from .base import *
import dj_database_url
import os

DEBUG = False

# Deben proporcionarse siempre en entorno de producción
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "").split(",")

# Exigir DATABASE_URL en producción
DATABASES = {
    "default": dj_database_url.config(conn_max_age=600, ssl_require=False) # Configurar ssl_require a True idealmente
}

# Producción requiere Redis para Caché y Celery
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/1")

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

# Configuraciones de Seguridad estrictas
SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "True") == "True"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# CORS
cors_env = os.environ.get("CORS_ALLOWED_ORIGINS")
if cors_env:
    CORS_ALLOWED_ORIGINS = cors_env.split(",")
else:
    CORS_ALLOWED_ORIGINS = []
