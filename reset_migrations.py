import os
import glob
import shutil

base_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

for path in glob.glob(os.path.join(base_dir, '**', 'migrations', '*.py'), recursive=True):
    if not path.endswith('__init__.py'):
        os.remove(path)
        print(f"Removed: {path}")

for path in glob.glob(os.path.join(base_dir, '**', 'migrations', '__pycache__'), recursive=True):
    shutil.rmtree(path, ignore_errors=True)

print("Migrations reset complete.")
