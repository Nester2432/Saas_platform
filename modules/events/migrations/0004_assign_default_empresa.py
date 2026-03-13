from django.db import migrations

def assign_default_empresa(apps, schema_editor):
    Empresa = apps.get_model('empresas', 'Empresa')
    EventStore = apps.get_model('events', 'EventStore')
    
    # Try to find existing Empresa Demo by name or slug
    empresa_demo = Empresa.objects.filter(nombre="Empresa Demo").first()
    if not empresa_demo:
        empresa_demo = Empresa.objects.filter(slug="demo").first()
    
    if not empresa_demo:
        # Create it if it truly doesn't exist (should not happen in this environment)
        empresa_demo = Empresa.objects.create(
            nombre="Empresa Demo",
            slug="demo",
            plan="free",
            is_active=True
        )
    
    # Update EventStore records is already handled by the default value in 0003_... migration.
    # EventStore.objects.filter(empresa__isnull=True).update(empresa=empresa_demo)
    
    # Also ensure any other core models with null empresa are assigned to demo
    # (Requirement 7: assign all existing records to a default Empresa)
    
    try:
        Cliente = apps.get_model('clientes', 'Cliente')
        Cliente.objects.filter(empresa__isnull=True).update(empresa=empresa_demo)
    except (LookupError, AttributeError):
        pass

    try:
        Venta = apps.get_model('ventas', 'Venta')
        Venta.objects.filter(empresa__isnull=True).update(empresa=empresa_demo)
    except (LookupError, AttributeError):
        pass

    try:
        Factura = apps.get_model('facturacion', 'Factura')
        Factura.objects.filter(empresa__isnull=True).update(empresa=empresa_demo)
    except (LookupError, AttributeError):
        pass

class Migration(migrations.Migration):
    dependencies = [
        ('events', '0003_remove_eventstore_events_even_empresa_262667_idx_and_more'),
        ('empresas', '0002_initial'),
    ]

    operations = [
        migrations.RunPython(assign_default_empresa, reverse_code=migrations.RunPython.noop),
    ]
