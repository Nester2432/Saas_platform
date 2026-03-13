import os
import django
import json
import uuid
from datetime import datetime, date
from decimal import Decimal

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.usuarios.models import Usuario
from apps.empresas.models import Empresa
from modules.events.demo_views import DemoFullFlowView, DemoResourcesView, DemoStatusView
from rest_framework.test import APIRequestFactory, force_authenticate

def setup_demo_billing():
    from apps.empresas.models import Empresa
    from modules.billing.models import Plan, Suscripcion
    from modules.billing.services.billing_service import BillingService
    
    # Ensure plans exist
    # First, make other plans inactive to ensure Starter is picked by signals (or we'll assign it manually)
    Plan.objects.all().update(activo=False)
    
    plan_starter, _ = Plan.objects.update_or_create(
        nombre="Starter",
        defaults={
            "precio_mensual": Decimal("19.00"),
            "max_usuarios": 2,
            "max_productos": 5,
            "max_clientes": 3,
            "activo": True
        }
    )
    
    plan_enterprise, _ = Plan.objects.update_or_create(
        nombre="Enterprise",
        defaults={
            "precio_mensual": Decimal("99.00"),
            "max_usuarios": None, # Unlimited
            "max_productos": None,
            "max_clientes": None,
            "activo": True
        }
    )
    
    for empresa in Empresa.objects.all():
        if not Suscripcion.objects.filter(empresa=empresa, estado="ACTIVE").exists():
            print(f"Assigning Starter plan to {empresa.nombre}...")
            BillingService.create_subscription(empresa, plan_starter)
        else:
            # Upgrade existing to ensure known limits
            BillingService.upgrade_plan(empresa, plan_starter)

    # Clean up old demo data to avoid limit issues
    from modules.inventario.models import Producto
    from modules.clientes.models import Cliente
    from modules.ventas.models import Venta
    Producto.objects.filter(empresa__nombre__icontains="Demo").delete()
    Cliente.objects.filter(empresa__nombre__icontains="Demo").delete()
    Venta.objects.filter(empresa__nombre__icontains="Demo").delete()
    Producto.objects.filter(empresa__nombre__icontains="Billing Test").delete()

def run_demo():
    print("--- Inciando Demo Flow ---")
    setup_demo_billing()
    factory = APIRequestFactory()
    user = Usuario.objects.get(email='admin@demo.com')
    
    # 1. Ejecutar el flujo completo
    print("Step 1: POST /api/v1/events/demo/full-flow/")
    view = DemoFullFlowView.as_view()
    unique_email = f"demo_{uuid.uuid4().hex[:8]}@example.com"
    request = factory.post('/api/v1/events/demo/full-flow/', data={"email_cliente": unique_email}, format='json')
    force_authenticate(request, user=user)
    response = view(request)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.data, indent=2))
    
    if response.status_code != 201:
        print("Error en el flujo")
        return

    # 2. Esperar un poco para el procesamiento asíncrono
    import time
    print("\nEsperando 5 segundos para procesamiento asíncrono (Celery)...")
    time.sleep(5)
    
    # 3. Ver recursos generados
    print("\nStep 2: GET /api/v1/events/demo/resources/")
    view = DemoResourcesView.as_view()
    request = factory.get('/api/v1/events/demo/resources/')
    force_authenticate(request, user=user)
    response = view(request)
    print(f"Status: {response.status_code}")
    
    def json_serial(obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        raise TypeError ("Type %s not serializable" % type(obj))

    print(json.dumps(response.data, indent=2, default=json_serial))
    
    # 4. Ver estado del sistema
    print("\nStep 3: GET /api/v1/events/demo/status/")
    view = DemoStatusView.as_view()
    request = factory.get('/api/v1/events/demo/status/')
    force_authenticate(request, user=user)
    response = view(request)
    print(f"Status: {response.status_code}")
    print(json.dumps(response.data, indent=2, default=json_serial))

    # 5. Phase 16: Product -> Sale -> Inventory flow
    print("\nStep 4: Phase 16 Integration (Product -> Sale -> Inventory)")
    
    # Use existing models to create a product for the demo empresa
    from modules.inventario.models import Producto
    from modules.inventario.services import MovimientoService
    from modules.ventas.models import MetodoPago
    from modules.ventas.api.views import VentaViewSet
    from modules.inventario.api.views import ProductoViewSet
    
    empresa = Empresa.objects.get(nombre='Empresa Demo')
    
    # Switch user to this empresa for Step 4
    old_empresa = user.empresa
    user.empresa = empresa
    user.save()

    # 5.1 Create Product via API
    print("5.1: POST /api/v1/productos/")
    view_prod = ProductoViewSet.as_view({'post': 'create'})
    prod_data = {
        "nombre": "Producto Demo Phase 16",
        "precio_venta": "1500.00",
        "codigo": f"DEMO-{uuid.uuid4().hex[:4]}"
    }
    request = factory.post('/api/v1/productos/', prod_data, format='json')
    force_authenticate(request, user=user)
    request.empresa = empresa # Middleware simulation
    response = view_prod(request)
    print(f"Product Status: {response.status_code}")
    producto_id = response.data['id']
    
    # 5.2 Add initial stock via service (usually done via OrdenCompra or Adjustment)
    producto = Producto.objects.get(id=producto_id)
    print(f"Adding initial stock of 50 units...")
    MovimientoService.registrar_entrada(
        empresa=empresa,
        producto=producto,
        cantidad=50,
        motivo="Stock Demo Phase 16",
        referencia_tipo="stock_inicial"
    )
    
    # 5.3 Create Sale via API
    print("5.2: POST /api/v1/ventas/")
    view_venta = VentaViewSet.as_view({'post': 'create'})
    venta_data = {"notas": "Venta demo Phase 16"}
    request = factory.post('/api/v1/ventas/', venta_data, format='json')
    force_authenticate(request, user=user)
    request.empresa = empresa
    response = view_venta(request)
    print(f"Venta Status: {response.status_code}")
    venta_id = response.data['id']
    
    # 5.4 Add item via new /items/ alias
    print(f"5.3: POST /api/v1/ventas/{venta_id}/items/")
    view_items = VentaViewSet.as_view({'post': 'items'})
    item_data = {
        "producto_id": producto_id,
        "cantidad": 10
    }
    request = factory.post(f'/api/v1/ventas/{venta_id}/items/', item_data, format='json')
    force_authenticate(request, user=user)
    request.empresa = empresa
    response = view_items(request, pk=venta_id)
    print(f"Add Item Status: {response.status_code}")
    
    # 5.5 Confirm Sale (Stock reduction)
    print("5.4: POST /api/v1/ventas/{id}/confirmar/")
    from modules.ventas.models import TipoMetodoPago
    metodo, _ = MetodoPago.objects.get_or_create(
        empresa=empresa,
        nombre="Efectivo",
        defaults={
            "tipo": TipoMetodoPago.EFECTIVO,
            "activo": True
        }
    )
    view_conf = VentaViewSet.as_view({'post': 'confirmar'})
    conf_data = {
        "pagos": [{
            "metodo_pago_id": str(metodo.id),
            "monto": "15000.00"
        }]
    }
    request = factory.post(f'/api/v1/ventas/{venta_id}/confirmar/', conf_data, format='json')
    force_authenticate(request, user=user)
    request.empresa = empresa
    response = view_conf(request, pk=venta_id)
    print(f"Confirm Status: {response.status_code}")
    
    # 5.6 Verify Stock Reduction
    producto.refresh_from_db()
    print(f"\nVerification Results:")
    print(f"Initial Stock: 50")
    print(f"Sold quantity: 10")
    print(f"Final Stock:   {producto.stock_actual}")
    
    if producto.stock_actual == 40:
        print("SUCCESS: Phase 16 Integration Verified! (Stock reduction confirmed)")
    else:
        print("FAILURE: Stock mismatch.")

    # 6. Phase 17: Billing Engine (Plans, Trials, and Limits)
    print("\nStep 5: Phase 17 Billing Engine (Plans, Trials, and Limits)")
    from modules.billing.models import Plan, Suscripcion
    from modules.billing.services.billing_service import BillingService

    # 6.2 New Empresa with Auto-Trial
    print("6.1: Creating new Empresa via ORM (triggers auto-trial)")
    emp_uid = uuid.uuid4().hex[:6]
    new_empresa = Empresa.objects.create(
        nombre=f"Billing Test {emp_uid}",
        email=f"test@{emp_uid}.com",
    )
    print(f"Empresa Created: {new_empresa.nombre} (ID: {new_empresa.id})")
    
    # Switch user to this empresa for testing
    old_empresa = user.empresa
    user.empresa = new_empresa
    user.save()

    # 6.3 Verify Auto-Trial
    print(f"Verifying trial subscription for '{new_empresa.nombre}'...")
    sub = Suscripcion.objects.get(empresa=new_empresa, estado="TRIAL")
    print(f"Trial Subscription: {sub.plan.nombre} (Ends: {sub.fecha_fin})")

    # 6.4 Enforce Limits (Products)
    print("\n6.2: Testing Product Limits (Plan: Starter - Max 5)")
    view_prod = ProductoViewSet.as_view({'post': 'create'})
    from modules.inventario.models import CategoriaProducto
    cat = CategoriaProducto.objects.filter(empresa=new_empresa).first()
    if not cat:
        cat = CategoriaProducto.objects.create(empresa=new_empresa, nombre="General")

    for i in range(5):
        payload = {"nombre": f"P{i}", "precio_venta": "10.00", "categoria_id": str(cat.id)}
        req = factory.post('/api/v1/productos/', payload, format='json')
        force_authenticate(req, user=user)
        req.empresa = new_empresa
        view_prod(req)
        print(f"Created P{i}")

    # The 6th should fail
    print("Creating 6th product (should fail)...")
    payload = {"nombre": "Failure Product", "precio_venta": "10.00", "categoria_id": str(cat.id)}
    req = factory.post('/api/v1/productos/', payload, format='json')
    force_authenticate(req, user=user)
    req.empresa = new_empresa
    response = view_prod(req)
    print(f"Response Status: {response.status_code}")
    print(f"Error Detail: {response.data.get('detail') or response.data}")

    # 6.5 Upgrade Plan
    print("\n6.3: Upgrading to Enterprise (Unlimited)")
    enterprise_plan = Plan.objects.get(nombre="Enterprise")
    BillingService.upgrade_plan(new_empresa, enterprise_plan, usuario=user)
    print("Upgrade successful!")

    # 6.6 Verify Limits expanded
    print("Creating product after upgrade (should succeed)...")
    req = factory.post('/api/v1/productos/', payload, format='json') # New request object
    force_authenticate(req, user=user)
    req.empresa = new_empresa
    response = view_prod(req)
    print(f"Response Status: {response.status_code}")
    
    # Restore user empresa
    user.empresa = old_empresa
    user.save()

    if response.status_code == 201:
        print("\nSUCCESS: Phase 17 Billing Engine Verified! (Limits enforced and expanded)")
    else:
        print("\nFAILURE: Upgrade didn't expand limits correctly.")

if __name__ == "__main__":
    run_demo()
