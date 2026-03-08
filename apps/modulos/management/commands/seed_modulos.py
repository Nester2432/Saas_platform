"""
apps/modulos/management/commands/seed_modulos.py

Seeds the platform with the default module definitions.
Run after initial deployment:

    python manage.py seed_modulos
"""

from django.core.management.base import BaseCommand
from apps.modulos.models import Modulo


MODULOS_INICIALES = [
    {
        "nombre": "Clientes (CRM)",
        "codigo": "clientes",
        "descripcion": "Gestión de clientes, notas y etiquetas.",
        "plan_minimo": "free",
        "icono": "users",
        "orden": 1,
    },
    {
        "nombre": "Turnos",
        "codigo": "turnos",
        "descripcion": "Agendamiento y gestión de citas.",
        "plan_minimo": "starter",
        "icono": "calendar",
        "orden": 2,
    },
    {
        "nombre": "Ventas",
        "codigo": "ventas",
        "descripcion": "Punto de venta y registro de ventas.",
        "plan_minimo": "starter",
        "icono": "shopping-cart",
        "orden": 3,
    },
    {
        "nombre": "Inventario",
        "codigo": "inventario",
        "descripcion": "Control de stock y movimientos.",
        "plan_minimo": "starter",
        "icono": "package",
        "orden": 4,
    },
    {
        "nombre": "Facturación",
        "codigo": "facturacion",
        "descripcion": "Generación de facturas electrónicas.",
        "plan_minimo": "professional",
        "icono": "file-text",
        "orden": 5,
    },
    {
        "nombre": "Notificaciones",
        "codigo": "notificaciones",
        "descripcion": "Envío de emails, SMS y WhatsApp.",
        "plan_minimo": "starter",
        "icono": "bell",
        "orden": 6,
    },
    {
        "nombre": "Reportes",
        "codigo": "reportes",
        "descripcion": "Analítica y reportes de negocio.",
        "plan_minimo": "professional",
        "icono": "bar-chart-2",
        "orden": 7,
    },
    {
        "nombre": "IA",
        "codigo": "ia",
        "descripcion": "Automatización con inteligencia artificial.",
        "plan_minimo": "enterprise",
        "icono": "cpu",
        "orden": 8,
    },
]


class Command(BaseCommand):
    help = "Seeds the database with the default platform modules."

    def handle(self, *args, **options):
        created = 0
        updated = 0

        for data in MODULOS_INICIALES:
            obj, was_created = Modulo.objects.update_or_create(
                codigo=data["codigo"],
                defaults=data,
            )
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f"  Created: {obj.nombre}"))
            else:
                updated += 1
                self.stdout.write(f"  Updated: {obj.nombre}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. {created} created, {updated} updated."
            )
        )
