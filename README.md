# Plataforma SaaS — Infraestructura Base

## Stack
- **Backend**: Django 5 + Django REST Framework
- **Base de Datos**: PostgreSQL
- **Caché**: Redis
- **Autenticación**: JWT (djangorestframework-simplejwt)
- **Tareas**: Celery + Redis (Event Bus)
- **Procesamiento Asíncrono**: `EVENT_BUS_ASYNC=True` (env)

---

## Arquitectura

### Estrategia Multi-Tenant
Base de datos compartida, esquema compartido — cada modelo de negocio tiene una FK a `empresa`.

El aislamiento de datos se aplica en **tres capas**:
1. **ORM**: La clase base `EmpresaModel` fuerza la FK, `SoftDeleteTenantManager` proporciona `.for_empresa()`.
2. **Middleware**: `TenantMiddleware` resuelve `request.empresa` desde el JWT.
3. **ViewSet**: `TenantQuerysetMixin.get_queryset()` auto-scopea cada consulta.

### Jerarquía de Modelos
```
BaseModel (UUID PK, timestamps, soft delete, audit)
└── EmpresaModel (BaseModel + empresa FK)
    ├── Cliente
    ├── Turno
    ├── Venta
    └── ... todos los modelos de negocio
```

### Activación de Módulos
Cada `Empresa` tiene registros de `EmpresaModulo` que controlan qué módulos están activos.
`ModuloActivoPermission` verifica esto en cada solicitud.

---

## Configuración (Setup)

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Configurar entorno
cp .env.example .env
# Editar DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY, REDIS_URL

# 3. Migrar
python manage.py migrate

# 4. Sembrar módulos (Seed)
python manage.py seed_modulos

# 5. Crear superusuario
python manage.py createsuperuser

# 6. Ejecutar Celery Worker (En una terminal separada)
celery -A config worker --loglevel=info

# 7. Ejecutar Django
python manage.py runserver
```

---

## Sistema de Eventos y Demo

La plataforma incluye un **Event Bus** robusto para procesamiento desacoplado y asíncrono.

### Características Clave
- **Entrega "At-least-once"**: Garantizada por la persistencia en `EventStore`.
- **Asíncrono**: Las tareas se despachan a workers de Celery.
- **Idempotencia**: Los handlers usan guardas basadas en caché para evitar el procesamiento duplicado.
- **Observabilidad**: Endpoints integrados y dashboard UI para monitorear la salud del sistema.

### 🚀 Ejecución de la Demo

1. Asegúrate de que **Redis** esté funcionando.
2. Inicia el **Celery Worker**: `celery -A config worker --loglevel=info`.
3. Inicia el **Servidor Django**: `python manage.py runserver`.
4. Abre el **Dashboard de Demo**:
   - URL: `http://localhost:8000/events/demo/dashboard/`
   - *Nota: Requiere inicio de sesión como superusuario o administrador de empresa.*
5. Haz clic en **"Ejecutar Demo Flow"** para disparar el ciclo completo: 
   `Crear Cliente` → `Confirmar Venta` → `Marcar como Pagada` → `Generar Factura` → `Notificar`.
6. Monitorea los resultados en tiempo real en las métricas y el log del dashboard.

---

## Estructura del Proyecto

```
saas_platform/
├── core/                   # Infraestructura (modelos abstractos, middleware, permisos)
│   ├── models.py           # BaseModel, EmpresaModel
│   ├── mixins.py           # TenantQuerysetMixin, AuditLogMixin
│   ├── exceptions.py       # Manjeador de excepciones personalizado DRF
│   ├── middleware/
│   │   └── tenant_middleware.py
│   ├── permissions/
│   │   └── base.py         # IsTenantAuthenticated, ModuloActivoPermission
│   ├── managers/
│   │   └── base.py         # SoftDeleteManager, SoftDeleteTenantManager
│   └── querysets/
│       └── base.py         # SoftDeleteQuerySet, TenantQuerySet
│
├── apps/                   # Aplicaciones core de la plataforma
│   ├── empresas/           # Empresa, EmpresaConfiguracion
│   ├── usuarios/           # CustomUser, Rol, Permiso
│   └── modulos/            # Modulo, EmpresaModulo
│
├── modules/                # Módulos de negocio
│   ├── events/             # Event Bus, Store y Dashboard de Demo
│   ├── clientes/
│   ├── turnos/
│   ├── ventas/
│   ├── inventario/
│   ├── facturacion/
│   ├── notificaciones/
│   ├── reportes/
│   └── ia/
│
├── config/                 # Configuración de Django, configuración de Celery
└── requirements.txt
```

---

## Añadiendo un Nuevo Módulo

1. Crea `modules/mymodule/` con: `models.py`, `serializers.py`, `views.py`, `services.py`, `permissions.py`, `urls.py`.
2. Hereda todos los modelos de `EmpresaModel`.
3. Usa `TenantQuerysetMixin` en los ViewSets.
4. Establece `modulo_requerido = "mymodule"` en los ViewSets.
5. Registra las urls en `config/urls.py`.
6. Añade el módulo a `MODULOS_INICIALES` en el comando `seed_modulos`.

---

## Patrones Clave

### Publicar un Evento
```python
from modules.events.event_bus import EventBus

EventBus.publish(
    event_name="venta_confirmada",
    empresa_id=venta.empresa_id,
    payload={"venta_id": str(venta.id)},
    usuario_id=request.user.id
)
```

### Registrar un Handler
Añade tu handler en `modules/events/event_bus.py` dentro del método `_get_handlers` (o en un registro automatizado futuro).

---

### Crear un modelo de negocio
```python
from core.models import EmpresaModel

class Cliente(EmpresaModel):
    nombre = models.CharField(max_length=200)
    # empresa, id, created_at, deleted_at, etc. heredados automáticamente
```

### Consultas seguras (Multi-Tenant)
```python
# Siempre filtrado por empresa, siempre excluye eliminados lógicamente (soft-deleted)
clientes = Cliente.objects.for_empresa(request.empresa)

# Incluir eliminados lógicamente
todos = Cliente.objects.with_deleted().for_empresa(request.empresa)
```

### ViewSet
```python
class ClienteViewSet(TenantQuerysetMixin, AuditLogMixin, viewsets.ModelViewSet):
    queryset = Cliente.objects.all()  # Filtrado automático por el mixin
    serializer_class = ClienteSerializer
    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido = "clientes"
```

### Prevenir doble reserva (secciones críticas)
```python
from django.db import transaction

@transaction.atomic
def reservar_turno(...):
    Turno.objects.select_for_update().filter(...)  # Bloqueo a nivel de fila
```
