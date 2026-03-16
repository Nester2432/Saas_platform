"""
fix_factories_v2.py
Fixes all test factories and conftest files to avoid IntegrityErrors and naming issues.
"""
import os
import re

BASE = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

files_to_fix = [
    os.path.join(BASE, "modules", "clientes", "tests", "factories.py"),
    os.path.join(BASE, "modules", "ventas", "tests", "factories.py"),
    os.path.join(BASE, "modules", "auditlog", "tests", "conftest.py"),
    os.path.join(BASE, "modules", "events", "tests", "conftest.py"),
]

def fix_file(path):
    if not os.path.exists(path):
        print(f"NOT FOUND: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    original = content

    # 1. Fix Suscripcion.objects.create(...) -> check/update existing
    # This regex matches the Suscripcion.objects.create( ... ) block
    # We replace it with a block that filters and updates or creates only if missing.
    
    # Patttern for auditlog/conftest.py style
    pattern_create = r'Suscripcion\.objects\.create\(\s*empresa=emp,\s*plan=plan_pro,\s*estado="ACTIVA",\s*fecha_inicio=timezone\.now\(\)\.date\(\)\s*\)'
    replacement_update = 'Suscripcion.objects.filter(empresa=emp).update(plan=plan_pro, estado=EstadoSuscripcion.ACTIVE, fecha_inicio=timezone.now().date())'
    content = re.sub(pattern_create, replacement_update, content)

    # Pattern for factories.py style
    pattern_create_fact = r'Suscripcion\.objects\.create\(\s*empresa=empresa,\s*plan=plan,\s*estado=EstadoSuscripcion\.ACTIVE,\s*fecha_inicio=timezone\.now\(\)\.date\(\)\s*\)'
    replacement_update_fact = 'Suscripcion.objects.filter(empresa=empresa).update(plan=plan, estado=EstadoSuscripcion.ACTIVE, fecha_inicio=timezone.now().date())'
    content = re.sub(pattern_create_fact, replacement_update_fact, content)

    # 2. Fix estado="ACTIVA" -> "ACTIVE" or EstadoSuscripcion.ACTIVE
    content = content.replace('estado="ACTIVA"', 'estado=EstadoSuscripcion.ACTIVE')
    
    # 3. Ensure Suscripcion is updated using update() if it already exists to avoid unique constraint
    # (The regex above already does some of this)

    if content != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"FIXED: {path}")
    else:
        print(f"NO CHANGE: {path}")

for f in files_to_fix:
    fix_file(f)

print("Done.")
