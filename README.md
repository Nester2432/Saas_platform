# SaaS Platform — Base Infrastructure

## Stack
- **Backend**: Django 5 + Django REST Framework
- **Database**: PostgreSQL
- **Cache**: Redis
- **Auth**: JWT (djangorestframework-simplejwt)
- **Tasks**: Celery + Redis

---

## Architecture

### Multi-Tenant Strategy
Shared database, shared schema — every business model has an `empresa` FK.

Data isolation is enforced at **three layers**:
1. **ORM**: `EmpresaModel` base class forces the FK, `SoftDeleteTenantManager` provides `.for_empresa()`
2. **Middleware**: `TenantMiddleware` resolves `request.empresa` from JWT
3. **ViewSet**: `TenantQuerysetMixin.get_queryset()` auto-scopes every query

### Model Hierarchy
```
BaseModel (UUID PK, timestamps, soft delete, audit)
└── EmpresaModel (BaseModel + empresa FK)
    ├── Cliente
    ├── Turno
    ├── Venta
    └── ... all business models
```

### Module Activation
Each `Empresa` has `EmpresaModulo` records controlling which modules are active.
`ModuloActivoPermission` checks this on every request.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit DB_NAME, DB_USER, DB_PASSWORD, SECRET_KEY, REDIS_URL

# 3. Migrate
python manage.py migrate

# 4. Seed modules
python manage.py seed_modulos

# 5. Create superuser
python manage.py createsuperuser

# 6. Run
python manage.py runserver
```

---

## Project Structure

```
saas_platform/
├── core/                   # Infrastructure (abstract models, middleware, permissions)
│   ├── models.py           # BaseModel, EmpresaModel
│   ├── mixins.py           # TenantQuerysetMixin, AuditLogMixin
│   ├── exceptions.py       # Custom DRF exception handler
│   ├── middleware/
│   │   └── tenant_middleware.py
│   ├── permissions/
│   │   └── base.py         # IsTenantAuthenticated, ModuloActivoPermission
│   ├── managers/
│   │   └── base.py         # SoftDeleteManager, SoftDeleteTenantManager
│   └── querysets/
│       └── base.py         # SoftDeleteQuerySet, TenantQuerySet
│
├── apps/                   # Core SaaS apps (no empresa FK — they ARE the foundation)
│   ├── empresas/           # Empresa, EmpresaConfiguracion
│   ├── usuarios/           # CustomUser, Rol, Permiso, JWT tokens
│   └── modulos/            # Modulo, EmpresaModulo
│
├── modules/                # Business modules (all inherit EmpresaModel)
│   ├── clientes/
│   ├── turnos/
│   ├── ventas/
│   ├── inventario/
│   ├── facturacion/
│   ├── notificaciones/
│   ├── reportes/
│   └── ia/
│
├── api/                    # Shared API utilities, versioning
├── config/                 # Django settings, URLs, WSGI
└── requirements.txt
```

---

## Adding a New Module

1. Create `modules/mymodule/` with: `models.py`, `serializers.py`, `views.py`, `services.py`, `permissions.py`, `urls.py`
2. Inherit all models from `EmpresaModel`
3. Use `TenantQuerysetMixin` in ViewSets
4. Set `modulo_requerido = "mymodule"` on ViewSets
5. Register urls in `config/urls.py`
6. Add module to `MODULOS_INICIALES` in `seed_modulos` command

---

## Key Patterns

### Creating a business model
```python
from core.models import EmpresaModel

class Cliente(EmpresaModel):
    nombre = models.CharField(max_length=200)
    # empresa, id, created_at, deleted_at, etc. all inherited
```

### Querying safely
```python
# Always scoped to empresa, always excludes soft-deleted
clientes = Cliente.objects.for_empresa(request.empresa)

# Include soft-deleted
todos = Cliente.objects.with_deleted().for_empresa(request.empresa)
```

### ViewSet
```python
class ClienteViewSet(TenantQuerysetMixin, AuditLogMixin, viewsets.ModelViewSet):
    queryset = Cliente.objects.all()  # auto-scoped by mixin
    serializer_class = ClienteSerializer
    permission_classes = [IsTenantAuthenticated, ModuloActivoPermission]
    modulo_requerido = "clientes"
```

### Preventing double-booking (critical sections)
```python
from django.db import transaction

@transaction.atomic
def reservar_turno(...):
    Turno.objects.select_for_update().filter(...)  # row-level lock
```
