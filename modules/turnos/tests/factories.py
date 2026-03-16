"""
modules/turnos/tests/factories.py

Lightweight factory helpers for the turnos test suite.

No factory_boy dependency — plain functions returning saved instances.
Each function accepts **kwargs to override any field.

Dependency chain:
    make_empresa()
        └─ make_usuario(empresa)         → platform user
        └─ make_admin(empresa)           → is_empresa_admin=True
        └─ make_servicio(empresa)        → a bookable service
        └─ make_profesional(empresa)     → a staff member
            └─ make_profesional_servicio(empresa, profesional, servicio)
            └─ make_horario(empresa, profesional, dia_semana, hora_inicio, hora_fin)
            └─ make_bloqueo(empresa, profesional, fecha_inicio, fecha_fin)
        └─ make_turno(empresa, profesional, servicio, fecha_inicio)

Module activation:
    activar_modulo(empresa, "turnos")   → required for API tests
    make_permiso(codigo)                → seeded Permiso row
    asignar_permiso(usuario, empresa, permiso_codigo) → grants permission via Rol
"""

import uuid
from datetime import time, timedelta

from django.utils import timezone

from apps.empresas.models import Empresa, EmpresaConfiguracion
from apps.modulos.models import Modulo, EmpresaModulo
from apps.usuarios.models import Usuario, Rol, Permiso

from modules.turnos.models import (
    ActorCancelacion,
    BloqueoHorario,
    DiaSemana,
    EstadoTurno,
    HorarioDisponible,
    Profesional,
    ProfesionalServicio,
    Servicio,
    Turno,
)
from modules.billing.models import Plan, Suscripcion, EstadoSuscripcion


# ─────────────────────────────────────────────────────────────────────────────
# Core platform factories (reused from clientes, kept here to avoid import dep)
# ─────────────────────────────────────────────────────────────────────────────

def make_empresa(**kwargs) -> Empresa:
    uid = uuid.uuid4().hex[:8]
    defaults = {
        "nombre": f"Empresa Test {uid}",
        "slug": f"empresa-{uid}",
        "email": f"admin@empresa-{uid}.com",
        "is_active": True,
    }
    defaults.update(kwargs)
    
    # Pre-create plan so signals find it
    plan, _ = Plan.objects.get_or_create(
        nombre="Test Plan",
        defaults={
            "precio_mensual": 0,
            "activo": True
        }
    )
    
    empresa = Empresa.objects.create(**defaults)
    EmpresaConfiguracion.objects.get_or_create(empresa=empresa)
    
    # Ensure active subscription
    Suscripcion.objects.filter(empresa=empresa).update(
        plan=plan, 
        estado=EstadoSuscripcion.ACTIVE, 
        fecha_inicio=timezone.now().date()
    )
    
    return empresa


def make_usuario(empresa: Empresa, **kwargs) -> Usuario:
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
        email=email, empresa=empresa, password=password, **defaults
    )


def make_admin(empresa: Empresa, **kwargs) -> Usuario:
    """Create a usuario with is_empresa_admin=True — bypasses all permission checks."""
    return make_usuario(empresa=empresa, is_empresa_admin=True, **kwargs)


def activar_modulo(empresa: Empresa, codigo: str) -> EmpresaModulo:
    """Activate a platform module for an empresa (required by ModuloActivoPermission)."""
    modulo, _ = Modulo.objects.get_or_create(
        codigo=codigo,
        defaults={"nombre": codigo.capitalize(), "plan_minimo": "free"},
    )
    em, _ = EmpresaModulo.objects.get_or_create(
        empresa=empresa, modulo=modulo, defaults={"activo": True}
    )
    if not em.activo:
        em.activo = True
        em.save(update_fields=["activo"])
    return em


# ─────────────────────────────────────────────────────────────────────────────
# Permission factories
# ─────────────────────────────────────────────────────────────────────────────

def make_permiso(codigo: str, **kwargs) -> Permiso:
    """Get or create a platform-wide Permiso row."""
    permiso, _ = Permiso.objects.get_or_create(
        codigo=codigo,
        defaults={
            "nombre": codigo.replace(".", " ").capitalize(),
            "modulo": codigo.split(".")[0],
            **kwargs,
        },
    )
    return permiso


def asignar_permiso(usuario: Usuario, empresa: Empresa, permiso_codigo: str) -> Rol:
    """
    Grant a permission to a user via a dedicated Rol.

    Creates a Rol named after the permiso code, creates the Permiso if
    needed, adds it to the Rol, and adds the Rol to the user.

    This is the minimal setup that makes usuario.tiene_permiso(codigo) return True.
    """
    permiso = make_permiso(permiso_codigo)
    rol, _ = Rol.objects.get_or_create(
        empresa=empresa,
        codigo=f"rol-{permiso_codigo.replace('.', '-')}",
        defaults={"nombre": f"Rol {permiso_codigo}"},
    )
    rol.permisos.add(permiso)
    usuario.roles.add(rol)
    return rol


def make_usuario_con_permisos(empresa: Empresa, permisos: list[str], **kwargs) -> Usuario:
    """Create a usuario and grant them a list of permission codes."""
    usuario = make_usuario(empresa=empresa, **kwargs)
    for codigo in permisos:
        asignar_permiso(usuario, empresa, codigo)
    return usuario


# ─────────────────────────────────────────────────────────────────────────────
# Turnos domain factories
# ─────────────────────────────────────────────────────────────────────────────

def make_servicio(empresa: Empresa, **kwargs) -> Servicio:
    """Create a Servicio with sensible defaults."""
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre": f"Servicio {uid}",
        "duracion_minutos": 60,
        "precio": "1500.00",
        "activo": True,
        "color": "#3B82F6",
    }
    defaults.update(kwargs)
    return Servicio.objects.create(empresa=empresa, **defaults)


def make_profesional(empresa: Empresa, usuario: Usuario = None, **kwargs) -> Profesional:
    """Create a Profesional. Pass usuario= to link a platform account."""
    uid = uuid.uuid4().hex[:6]
    defaults = {
        "nombre": "Prof",
        "apellido": f"Test {uid}",
        "email": f"prof-{uid}@test.com",
        "activo": True,
        "color_agenda": "#10B981",
    }
    defaults.update(kwargs)
    return Profesional.objects.create(empresa=empresa, usuario=usuario, **defaults)


def make_profesional_servicio(
    empresa: Empresa,
    profesional: Profesional,
    servicio: Servicio,
    **kwargs,
) -> ProfesionalServicio:
    """Assign a service to a professional (creates ProfesionalServicio join)."""
    defaults = {"duracion_override": None}
    defaults.update(kwargs)
    return ProfesionalServicio.objects.create(
        empresa=empresa,
        profesional=profesional,
        servicio=servicio,
        **defaults,
    )


def make_horario(
    empresa: Empresa,
    profesional: Profesional,
    dia_semana: int = DiaSemana.LUNES,
    hora_inicio: time = time(9, 0),
    hora_fin: time = time(18, 0),
    **kwargs,
) -> HorarioDisponible:
    """Create a recurring weekly schedule block for a professional."""
    defaults = {"activo": True}
    defaults.update(kwargs)
    return HorarioDisponible.objects.create(
        empresa=empresa,
        profesional=profesional,
        dia_semana=dia_semana,
        hora_inicio=hora_inicio,
        hora_fin=hora_fin,
        **defaults,
    )


def make_bloqueo(
    empresa: Empresa,
    profesional: Profesional,
    fecha_inicio=None,
    fecha_fin=None,
    **kwargs,
) -> BloqueoHorario:
    """Create a one-off schedule block (absence, holiday, etc.)."""
    now = timezone.now()
    defaults = {
        "motivo": "Bloqueo de prueba",
        "fecha_inicio": fecha_inicio or now,
        "fecha_fin": fecha_fin or (now + timedelta(hours=2)),
    }
    defaults.update(kwargs)
    return BloqueoHorario.objects.create(
        empresa=empresa, profesional=profesional, **defaults
    )


def make_turno(
    empresa: Empresa,
    profesional: Profesional,
    servicio: Servicio,
    fecha_inicio=None,
    **kwargs,
) -> Turno:
    """
    Create a Turno directly in DB (bypassing TurnoService).

    Use for test setup when you need a turno to exist without going
    through the full service validation flow.
    For testing the creation flow itself, call TurnoService.crear_turno().
    """
    now = timezone.now()
    fi = fecha_inicio or (now + timedelta(days=1, hours=2))
    duracion = servicio.duracion_minutos
    ff = fi + timedelta(minutes=duracion)
    defaults = {
        "estado": EstadoTurno.PENDIENTE,
        "fecha_inicio": fi,
        "fecha_fin": ff,
        "notas_cliente": "",
        "notas_internas": "",
    }
    defaults.update(kwargs)
    return Turno.objects.create(
        empresa=empresa,
        profesional=profesional,
        servicio=servicio,
        **defaults,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Compound setup helper
# ─────────────────────────────────────────────────────────────────────────────

def setup_turno_completo(empresa: Empresa = None, dia_semana: int = None):
    """
    Build the full dependency graph needed to book a turno:
        empresa → profesional → servicio → ProfesionalServicio → HorarioDisponible

    Returns a dict with all created objects.

    dia_semana defaults to the weekday of "tomorrow" so tests can book
    without needing to know what day of the week tests run on.
    """
    empresa = empresa or make_empresa()
    admin = make_admin(empresa)
    servicio = make_servicio(empresa, duracion_minutos=60)
    profesional = make_profesional(empresa)
    make_profesional_servicio(empresa, profesional, servicio)

    # Default to tomorrow's weekday so "tomorrow at 10:00" is always inside
    # the professional's working hours — no test failures based on day of week.
    from datetime import date
    tomorrow = date.today() + timedelta(days=1)
    ws = dia_semana if dia_semana is not None else tomorrow.weekday()

    make_horario(empresa, profesional, dia_semana=ws, hora_inicio=time(8, 0), hora_fin=time(20, 0))

    return {
        "empresa": empresa,
        "admin": admin,
        "servicio": servicio,
        "profesional": profesional,
        "dia_semana": ws,
    }
