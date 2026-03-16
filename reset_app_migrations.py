import os
import glob
import shutil

base_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

folders_to_clean = ['apps', 'modules', 'core']

for f in folders_to_clean:
    folder_path = os.path.join(base_dir, f)
    for path in glob.glob(os.path.join(folder_path, '**', 'migrations', '*.py'), recursive=True):
        if not path.endswith('__init__.py') and 'venv' not in path:
            os.remove(path)
            print(f"Removed: {path}")

    for path in glob.glob(os.path.join(folder_path, '**', 'migrations', '__pycache__'), recursive=True):
        if 'venv' not in path:
            shutil.rmtree(path, ignore_errors=True)

print("App migrations reset complete.")
