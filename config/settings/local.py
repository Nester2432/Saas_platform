from .base import *
import dj_database_url
import os

DEBUG = True
TESTING = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# Conexión local a la base de datos PostgreSQL via dj_database_url
# Opciones por defecto para desarrollo local si la variable no está seteada:
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://saas_user:saas_password@127.0.0.1:5432/saas_db"
)

DATABASES = {
    "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)
}

# Usar el caché local en formato dummy o locmem para no depender siempre de Redis
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "unique-snowflake-local",
    }
}

# O si queremos testear Redis en modo local:
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CACHES["default"] = {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "TIMEOUT": 300,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }

# CORS - permitir los puertos típicos de frontend local
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
]
