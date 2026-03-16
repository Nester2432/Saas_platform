import os
import glob
import re

base_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform"

models_path = os.path.join(base_dir, 'apps', 'empresas', 'models.py')
with open(models_path, 'r', encoding='utf-8') as f:
    content = f.read()

content = re.sub(r'    class Plan\(models\.TextChoices\):.*?(?=\s+nombre = models\.)', '', content, flags=re.DOTALL)
content = re.sub(r'    plan = models\.CharField\(\s*max_length=20,\s*choices=Plan\.choices,\s*default=Plan\.\w+,\s*\)\n', '', content, flags=re.DOTALL)

with open(models_path, 'w', encoding='utf-8') as f:
    f.write(content)
print(f"Cleaned {models_path}")

for path in glob.glob(os.path.join(base_dir, '**', 'factories.py'), recursive=True):
    if 'venv' in path: continue
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    new_content = re.sub(r'\s*"plan":\s*Empresa\.Plan\.\w+,?\n', '\n', content)
    
    if new_content != content:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Cleaned {path}")
