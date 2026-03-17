#!/usr/bin/env bash
# build.sh — Ejecutado por Render antes de iniciar el servicio
set -o errexit

echo "==> Instalando dependencias..."
pip install -r requirements.txt

echo "==> Colectando archivos estáticos..."
python manage.py collectstatic --noinput

echo "==> Aplicando migraciones..."
python manage.py migrate --noinput

echo "==> Ejecutando seed de demo (si es la primera vez)..."
python manage.py seed_demo || echo "Seed ya ejecutado o error no crítico, continuando..."

echo "==> Build completado exitosamente"
