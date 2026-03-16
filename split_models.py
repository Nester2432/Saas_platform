import os
import re

file_path = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\inventario\models.py"
models_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\inventario\models"
os.makedirs(models_dir, exist_ok=True)

with open(file_path, "r", encoding="utf-8") as f:
    text = f.read()

def get_block(regex):
    match = re.search(regex, text, re.DOTALL)
    if not match: 
        print(f"FAILED REGEX: {regex}")
        return ""
    return match.group(1).strip()

tipo_movimiento = get_block(r'(class TipoMovimiento.*?)(?=class EstadoOrdenCompra)')
estado_orden = get_block(r'(class EstadoOrdenCompra.*?)(?=\n# ─+\n)')
categoria = get_block(r'(class CategoriaProducto.*?)(?=\n# ─+\n)')
producto = get_block(r'(class Producto.*?)(?=\n# ─+\n)')
movimiento = get_block(r'(class MovimientoStock.*?)(?=\n# ─+\n)')
proveedor = get_block(r'(class Proveedor.*?)(?=\n# ─+\n)')
orden = get_block(r'(class OrdenCompra\(EmpresaModel\):.*?)(?=\n# ─+\n)')
detalle_orden = get_block(r'(class OrdenCompraDetalle.*?)$')

catalogo_content = f"""from django.db import models
from core.models import EmpresaModel

{categoria}


{producto}
"""
with open(os.path.join(models_dir, "catalogo.py"), "w", encoding="utf-8") as f: f.write(catalogo_content)

movimientos_content = f"""from django.db import models
from core.models import EmpresaModel
from django.core.exceptions import ValidationError
from .catalogo import Producto

{tipo_movimiento}


{movimiento}
"""
with open(os.path.join(models_dir, "movimientos.py"), "w", encoding="utf-8") as f: f.write(movimientos_content)

compras_content = f"""from django.db import models
from core.models import EmpresaModel
from django.core.exceptions import ValidationError
from .catalogo import Producto

{estado_orden}


{proveedor}


{orden}


{detalle_orden}
"""
with open(os.path.join(models_dir, "compras.py"), "w", encoding="utf-8") as f: f.write(compras_content)

init_content = """from .catalogo import CategoriaProducto, Producto
from .movimientos import TipoMovimiento, MovimientoStock
from .compras import EstadoOrdenCompra, Proveedor, OrdenCompra, OrdenCompraDetalle

__all__ = [
    "CategoriaProducto",
    "Producto",
    "TipoMovimiento",
    "MovimientoStock",
    "EstadoOrdenCompra",
    "Proveedor",
    "OrdenCompra",
    "OrdenCompraDetalle",
]
"""
with open(os.path.join(models_dir, "__init__.py"), "w", encoding="utf-8") as f: f.write(init_content)

print("Split completed successfully.")
