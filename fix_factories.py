"""
fix_factories.py
Fixes all test factories that use obsolete Plan/Suscripcion fields.
"""
import os
import re

BASE = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

files_to_fix = [
    os.path.join(BASE, "modules", "clientes", "tests", "factories.py"),
    os.path.join(BASE, "modules", "ventas", "tests", "factories.py"),
]

REPLACEMENTS = [
    # Fix slug lookup to use nombre
    (
        r'Plan\.objects\.get_or_create\(\s*slug="test-plan",\s*defaults=\{',
        'Plan.objects.get_or_create(\n        nombre="Test Plan",\n        defaults={\n'
    ),
    # Fix obsolete EstadoSuscripcion.ACTIVA → ACTIVE
    ("EstadoSuscripcion.ACTIVA", "EstadoSuscripcion.ACTIVE"),
]

for path in files_to_fix:
    if not os.path.exists(path):
        print(f"NOT FOUND: {path}")
        continue
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    original = content
    for pattern, replacement in REPLACEMENTS:
        if pattern.startswith("(") or "*" in pattern:
            content = re.sub(pattern, replacement, content, flags=re.DOTALL)
        else:
            content = content.replace(pattern, replacement)
    if content != original:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"FIXED: {path}")
    else:
        print(f"NO CHANGE: {path}")

# Also fix billing/api/views.py that uses limite_usuarios
billing_views = os.path.join(BASE, "modules", "billing", "api", "views.py")
if os.path.exists(billing_views):
    with open(billing_views, "r", encoding="utf-8") as f:
        content = f.read()
    new_content = content.replace("plan.limite_usuarios", "plan.max_usuarios")
    if new_content != content:
        with open(billing_views, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"FIXED billing views: {billing_views}")

print("Done.")
