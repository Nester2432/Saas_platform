"""
modules/clientes/tests/factories.py

Lightweight factory helpers for the clientes test suite.

Deliberately kept simple — no factory_boy dependency required.
Each factory function returns a fully saved instance with sensible defaults
that can be overridden via keyword arguments.

Usage:
    empresa   = make_empresa()
    usuario   = make_usuario(empresa=empresa)
    cliente   = make_cliente(empresa=empresa)
    etiqueta  = make_etiqueta(empresa=empresa)

    # Override any field:
    cliente = make_cliente(empresa=empresa, email="test@example.com", activo=False)
"""

import uuid
from apps.empresas.models import Empresa, EmpresaConfiguracion
from apps.usuarios.models import Usuario
from apps.modulos.models import Modulo, EmpresaModulo
from modules.clientes.models import Cliente, EtiquetaCliente, NotaCliente


# ---------------------------------------------------------------------------
# Core factories
# ---------------------------------------------------------------------------

from modules.billing.models import Plan, Suscripcion, EstadoSuscripcion, EstadoSuscripcion
from django.utils import timezone

def make_empresa(**kwargs) -> Empresa:
    """
    Create and return a saved Empresa instance.
    Each call generates a unique slug so parallel tests don't collide.
    Now also creates a default active subscription.
    """
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre": f"Empresa Test {uid}",
        "slug": f"empresa-test-{uid}",
        "email": f"admin@empresa-{uid}.com",
        "is_active": True,
    }
    defaults.update(kwargs)
    plan, _ = Plan.objects.get_or_create(
        nombre="Test Plan",
        defaults={

            "nombre": "Test Plan",
            "precio_mensual": 0,
            "activo": True
        }
    )
    empresa = Empresa.objects.create(**defaults)

    # Every empresa needs a configuracion for TenantMiddleware select_related
    EmpresaConfiguracion.objects.get_or_create(empresa=empresa)

    # NEW: Create a default Plan and Suscripcion so SubscriptionGuardMiddleware doesn't block tests
    Suscripcion.objects.filter(empresa=empresa).update(plan=plan, estado="ACTIVE", fecha_inicio=timezone.now().date())

    return empresa


def make_usuario(empresa: Empresa, **kwargs) -> Usuario:
    """
    Create and return a saved Usuario belonging to the given empresa.
    Password defaults to 'testpass123' — use authenticate() in tests that need it.
    """
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre": "Test",
        "apellido": "Usuario",
        "is_active": True,
        "is_empresa_admin": False,
    }
    defaults.update(kwargs)
    email = defaults.pop("email", f"usuario-{uid}@test.com")
    password = defaults.pop("password", "testpass123")
    return Usuario.objects.create_user(
        email=email,
        empresa=empresa,
        password=password,
        **defaults,
    )


def make_admin(empresa: Empresa, **kwargs) -> Usuario:
    """Create a usuario with is_empresa_admin=True."""
    return make_usuario(empresa=empresa, is_empresa_admin=True, **kwargs)


def make_cliente(empresa: Empresa, **kwargs) -> Cliente:
    """Create and return a saved Cliente for the given empresa."""
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre": "Cliente",
        "apellido": f"Test {uid}",
        "email": f"cliente-{uid}@test.com",
        "telefono": f"+549111{uid[:7]}",
        "activo": True,
    }
    defaults.update(kwargs)
    return Cliente.objects.create(empresa=empresa, **defaults)


def make_etiqueta(empresa: Empresa, **kwargs) -> EtiquetaCliente:
    """Create and return a saved EtiquetaCliente for the given empresa."""
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre": f"Tag {uid}",
        "color": "#3B82F6",
    }
    defaults.update(kwargs)
    return EtiquetaCliente.objects.create(empresa=empresa, **defaults)


def make_nota(empresa: Empresa, cliente: Cliente, usuario: Usuario = None, **kwargs) -> NotaCliente:
    """Create and return a saved NotaCliente for the given cliente."""
    defaults = {
        "contenido": "Nota de prueba para el cliente.",
        "created_by": usuario,
        "updated_by": usuario,
    }
    defaults.update(kwargs)
    return NotaCliente.objects.create(empresa=empresa, cliente=cliente, **defaults)


# ---------------------------------------------------------------------------
# Module activation helper
# ---------------------------------------------------------------------------

def activar_modulo(empresa: Empresa, codigo: str) -> EmpresaModulo:
    """
    Activate a module for an empresa.
    Creates the Modulo platform record if it doesn't exist yet.

    Used by API tests that go through ModuloActivoPermission.
    """
    modulo, _ = Modulo.objects.get_or_create(
        codigo=codigo,
        defaults={
            "nombre": codigo.capitalize(),
            "plan_minimo": "free",
        },
    )
    empresa_modulo, _ = EmpresaModulo.objects.get_or_create(
        empresa=empresa,
        modulo=modulo,
        defaults={"activo": True},
    )
    if not empresa_modulo.activo:
        empresa_modulo.activo = True
        empresa_modulo.save(update_fields=["activo"])
    return empresa_modulo
