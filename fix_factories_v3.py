"""
fix_factories_v3.py
Fixes NameErrors and IntegrityErrors in tests.
"""
import os

BASE = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

files_to_fix = [
    os.path.join(BASE, "modules", "clientes", "tests", "factories.py"),
    os.path.join(BASE, "modules", "ventas", "tests", "factories.py"),
    os.path.join(BASE, "modules", "auditlog", "tests", "conftest.py"),
    os.path.join(BASE, "modules", "events", "tests", "conftest.py"),
]

for path in files_to_fix:
    if not os.path.exists(path):
        continue
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Replace the constant with string value to avoid NameError if not imported
    # Also ensure we use "ACTIVE" (string) which is what TextChoices uses.
    content = content.replace("EstadoSuscripcion.ACTIVE", '"ACTIVE"')
    content = content.replace('estado="ACTIVA"', 'estado="ACTIVE"')
    
    # Fix the update logic to be sure it doesn't fail if Subscription missing (unlikely due to signal)
    # But for safety, we can use a more generic form.
    # The current form in v2 was: 
    # Suscripcion.objects.filter(empresa=emp).update(plan=plan_pro, estado=EstadoSuscripcion.ACTIVE, ... )
    
    # If the file doesn't have EstadoSuscripcion imported, we need to fix it.
    if 'EstadoSuscripcion' in content and 'import EstadoSuscripcion' not in content:
        # Check if it's imported from modules.billing.models
        if 'from modules.billing.models import Plan, Suscripcion' in content:
            content = content.replace(
                'from modules.billing.models import Plan, Suscripcion',
                'from modules.billing.models import Plan, Suscripcion, EstadoSuscripcion'
            )
            
    # Final cleanup of any left-over NameErrors from previous attempts
    content = content.replace('estado=EstadoSuscripcion.ACTIVE', 'estado="ACTIVE"')

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"CLEANED: {path}")

# Special fix for auditlog/tests/conftest.py which I saw in the traceback
audit_conftest = os.path.join(BASE, "modules", "auditlog", "tests", "conftest.py")
if os.path.exists(audit_conftest):
    with open(audit_conftest, "r", encoding="utf-8") as f:
        c = f.read()
    # It might have both "ACTIVA" and the failed replacement
    c = c.replace('estado="ACTIVA"', 'estado="ACTIVE"')
    with open(audit_conftest, "w", encoding="utf-8") as f:
        f.write(c)
