"""
management command: seed_demo

Creates demo data for first-time deploys:
- Demo Empresa
- Demo Plan (Pro)
- Demo Admin User

Safe to run multiple times (idempotent).

Usage:
    python manage.py seed_demo
"""
import uuid
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Seed demo data: empresa, plan, usuario admin"

    def handle(self, *args, **options):
        User = get_user_model()

        self.stdout.write(self.style.MIGRATE_HEADING("==> Seeding demo data..."))

        # 1. Empresa Demo
        from apps.empresas.models import Empresa
        empresa, created = Empresa.objects.get_or_create(
            nombre="Empresa Demo",
            defaults={
                "email": "demo@empresa.com",
                "is_active": True,
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"  ✓ Empresa creada: {empresa.nombre}"))
        else:
            self.stdout.write(f"  - Empresa ya existe: {empresa.nombre}")

        # 2. Plan Demo
        from modules.billing.models import Plan
        plan, created = Plan.objects.get_or_create(
            nombre="Plan Demo Pro",
            defaults={
                "precio_mensual": 0,
                "precio_anual": 0,
                "max_usuarios": 1000,
                "max_clientes": 1000,
                "max_productos": 1000,
                "rate_limit_per_minute": 600,
            }
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f"  ✓ Plan creado: {plan.nombre}"))
        else:
            self.stdout.write(f"  - Plan ya existe: {plan.nombre}")

        # 3. Suscripción activa
        from modules.billing.models import Suscripcion, EstadoSuscripcion, PeriodoFacturacion
        from django.utils import timezone

        if not Suscripcion.objects.filter(empresa=empresa).exists():
            Suscripcion.objects.create(
                empresa=empresa,
                plan=plan,
                estado=EstadoSuscripcion.ACTIVE,
                fecha_inicio=timezone.now().date(),
                periodo_facturacion=PeriodoFacturacion.MONTHLY,
            )
            self.stdout.write(self.style.SUCCESS(f"  ✓ Suscripción activa creada"))
        else:
            self.stdout.write(f"  - Suscripción ya existe")

        # 4. Usuario superadmin de plataforma
        DEMO_EMAIL = "admin@demo.com"
        DEMO_PASSWORD = "DemoPass2024!"

        if not User.objects.filter(email=DEMO_EMAIL).exists():
            admin = User.objects.create_superuser(
                email=DEMO_EMAIL,
                password=DEMO_PASSWORD,
            )
            # Asigna nombre y empresa al admin si tu modelo lo soporta
            admin.nombre = "Admin"
            admin.apellido = "Demo"
            admin.empresa = empresa
            admin.save()
            self.stdout.write(self.style.SUCCESS(
                f"  ✓ Usuario admin creado: {DEMO_EMAIL} / {DEMO_PASSWORD}"
            ))
        else:
            self.stdout.write(f"  - Usuario admin ya existe: {DEMO_EMAIL}")

        self.stdout.write(self.style.SUCCESS("\n==> ¡Demo data lista! La plataforma está preparada."))
