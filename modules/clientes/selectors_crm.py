"""
modules/clientes/selectors_crm.py

Selectors for CRM-specific data aggregation.
These functions encapsulate complex queries involving annotations and multi-module joins,
ensuring tenant isolation is always enforced.
"""

from django.db.models import Count, Max, Q, OuterRef, Subquery, Value, CharField
from django.db.models.functions import Coalesce
from modules.clientes.models import Cliente, HistorialCliente
from modules.ventas.models import Venta
from modules.turnos.models import Turno
from modules.facturacion.models import Factura

def get_contactos_queryset(tenant, search=None, ordering=None):
    """
    Returns a tenant-safe Cliente queryset annotated with CRM metrics.
    
    Metrics:
        - total_ventas: Count of non-cancelled sales.
        - total_turnos: Count of non-cancelled appointments.
        - ultima_interaccion: Max(created_at) among ventas, turnos and historial.
    """
    qs = Cliente.objects.filter(empresa=tenant, deleted_at__isnull=True)

    # Subqueries for last dates to compute ultima_interaccion
    last_venta = Venta.objects.filter(cliente=OuterRef('pk'), empresa=tenant).order_by('-created_at').values('created_at')[:1]
    last_turno = Turno.objects.filter(cliente=OuterRef('pk'), empresa=tenant).order_by('-created_at').values('created_at')[:1]
    last_hist  = HistorialCliente.objects.filter(cliente=OuterRef('pk'), empresa=tenant).order_by('-created_at').values('created_at')[:1]

    from django.db.models.functions import Greatest
    qs = qs.annotate(
        total_ventas=Count('ventas', filter=~Q(ventas__estado='CANCELADA')),
        total_turnos=Count('turnos', filter=~Q(turnos__estado='CANCELADO')),
        _last_v=Subquery(last_venta),
        _last_t=Subquery(last_turno),
        _last_h=Subquery(last_hist),
    )
    
    # Compute the max date among all activity
    qs = qs.annotate(
        ultima_interaccion=Greatest('created_at', '_last_h', '_last_v', '_last_t')
    )

    if search:
        qs = qs.filter(
            Q(nombre__icontains=search) |
            Q(apellido__icontains=search) |
            Q(email__icontains=search) |
            Q(telefono__icontains=search)
        )

    if ordering:
        valid_orders = ['ultima_interaccion', '-ultima_interaccion', 'created_at', '-created_at', 'nombre', '-nombre']
        if ordering in valid_orders:
            qs = qs.order_by(ordering)
    else:
        qs = qs.order_by('-ultima_interaccion')

    return qs.prefetch_related('etiquetas')

def get_contacto_360(cliente_id, tenant):
    """
    Retrieves full aggregated view for a single client.
    Ensures absolute tenant isolation for all related entities.
    """
    cliente = Cliente.objects.filter(id=cliente_id, empresa=tenant, deleted_at__isnull=True).prefetch_related('etiquetas').first()
    if not cliente:
        return None

    # Fetch relations in separate efficient queries (all tenant-scoped)
    ventas = Venta.objects.filter(cliente=cliente, empresa=tenant).order_by('-created_at')
    turnos = Turno.objects.filter(cliente=cliente, empresa=tenant).select_related('servicio', 'profesional').order_by('-fecha_inicio')
    
    # Facturas are linked to Ventas, but we filter by Empresa for safety
    facturas = Factura.objects.filter(venta__cliente=cliente, empresa=tenant).order_by('-created_at')
    
    # Activity from HistorialCliente
    actividad = HistorialCliente.objects.filter(cliente=cliente, empresa=tenant).order_by('-created_at')[:50]

    return {
        "cliente": cliente,
        "ventas": ventas,
        "turnos": turnos,
        "facturas": facturas,
        "actividad": actividad
    }
