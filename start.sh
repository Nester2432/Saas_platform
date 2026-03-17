#!/usr/bin/env bash
# start.sh — Comando de inicio del servidor en Render
set -o errexit

echo "==> Iniciando servidor con Gunicorn..."
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:${PORT:-8000} \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info
