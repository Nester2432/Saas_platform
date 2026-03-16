"""
fix_factories_v4.py
Swaps Plan and Empresa creation order to ensure signals work correctly.
"""
import os
import re

BASE = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

files_to_fix = [
    os.path.join(BASE, "modules", "clientes", "tests", "factories.py"),
    os.path.join(BASE, "modules", "ventas", "tests", "factories.py"),
    os.path.join(BASE, "modules", "inventario", "tests", "factories.py"),
]

def fix_file(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # We want to move the Plan.objects.get_or_create block ABOVE Empresa.objects.create
    
    # 1. Identify the Plan block
    plan_regex = r'(plan, _ = Plan\.objects\.get_or_create\(.*?\n    \))'
    # 2. Identify the Empresa block
    empresa_regex = r'(empresa = Empresa\.objects\.create\(.*?\))'
    
    plan_match = re.search(plan_regex, content, re.DOTALL)
    empresa_match = re.search(empresa_regex, content, re.DOTALL)
    
    if plan_match and empresa_match:
        plan_block = plan_match.group(1)
        empresa_block = empresa_match.group(1)
        
        # Check if plan is already before empresa
        if content.find(plan_block) < content.find(empresa_block):
            print(f"ALREADY CORRECT: {path}")
            return

        # Simple swap is risky if there are dependencies between them (usually there aren't besides plan needing to exist)
        # I'll manually construct the replacement to be safe for these specific files
        
        # In these files, make_empresa usually looks like:
        # uid = ...
        # defaults = ...
        # empresa = Empresa.objects.create(...)
        # ...
        # plan = Plan.objects.get_or_create(...)
        
        # I'll just find the plan block and move it before empresa creation line
        new_content = content.replace(plan_block + "\n", "") # Remove it first
        
        # Insert it before the empresa creation line
        new_content = new_content.replace(empresa_block, plan_block + "\n    " + empresa_block)
        
        if new_content != content:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"SWAPPED: {path}")
        else:
            print(f"NO CHANGE: {path}")

for f in files_to_fix:
    fix_file(f)

print("Done.")
