import os
import glob
import re

base_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

pagos_dir = os.path.join(base_dir, 'modules', 'pagos')
cobranzas_dir = os.path.join(base_dir, 'modules', 'cobranzas')

if os.path.exists(pagos_dir):
    os.rename(pagos_dir, cobranzas_dir)
    print("Renamed modules/cobranzas to modules/cobranzas")

for path in glob.glob(os.path.join(base_dir, '**', '*.py'), recursive=True):
    if 'venv' in path or '.git' in path or '__pycache__' in path or 'migrations' in path:
        continue
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()

        new_content = content.replace('modules.cobranzas', 'modules.cobranzas')
        new_content = new_content.replace('modules/cobranzas', 'modules/cobranzas')
        new_content = new_content.replace('api/v1/cobranzas', 'api/v1/cobranzas')
        new_content = new_content.replace('CobranzasConfig', 'CobranzasConfig')

        if new_content != content:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"Updated {path}")
    except Exception as e:
        print(f"Failed to process {path}: {e}")

# We should also rename the remaining files if any, but `modules.cobranzas` is the main goal.
