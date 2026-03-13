import os

def fix_imports(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                new_content = content.replace(
                    'modules.clientes.api.views', 'modules.clientes.api.views'
                ).replace(
                    'modules.clientes.api.serializers', 'modules.clientes.api.serializers'
                ).replace(
                    '.serializers', '.serializers' # no-op just for spacing
                ).replace(
                    'modules.clientes.api.permissions', 'modules.clientes.api.permissions'
                ).replace(
                    'modules.ventas.urls', 'modules.ventas.urls'
                )
                
                if content != new_content:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    print(f"Updated {path}")

fix_imports('.')
print("Import fix complete.")
