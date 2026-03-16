import os
import re

file_path = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\models.py"
models_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\models"
os.makedirs(models_dir, exist_ok=True)

with open(file_path, "r", encoding="utf-8") as f:
    text = f.read()

def get_block(regex):
    match = re.search(regex, text, re.DOTALL)
    if not match: 
        print(f"FAILED: {regex}")
        return ""
    return match.group(1).strip()

estado_venta = get_block(r'(class EstadoVenta.*?)(?=class TipoMetodoPago)')
tipo_metodo_pago = get_block(r'(class TipoMetodoPago.*?)(?=\n# ─+\n)')
secuencia_venta = get_block(r'(class SecuenciaVenta.*?)(?=\n# ─+\n)')
metodo_pago = get_block(r'(class MetodoPago.*?)(?=\n# ─+\n)')
venta = get_block(r'(class Venta\(EmpresaModel\):.*?)(?=\n# ─+\n)')
linea_venta = get_block(r'(class LineaVenta\(EmpresaModel\):.*?)(?=\n# ─+\n)')
pago_venta = get_block(r'(class PagoVenta\(EmpresaModel\):.*?)(?=\n# ─+\n)')
devolucion_venta = get_block(r'(class DevolucionVenta\(EmpresaModel\):.*?)(?=\n# ─+\n)')
devolucion_linea = get_block(r'(class DevolucionLineaVenta\(EmpresaModel\):.*?)$')

core_content = f"""from django.db import models
from core.models import EmpresaModel

{estado_venta}


{tipo_metodo_pago}


{secuencia_venta}


{metodo_pago}


{venta}
"""
with open(os.path.join(models_dir, "core.py"), "w", encoding="utf-8") as f: f.write(core_content)

lineas_content = f"""from django.db import models
from core.models import EmpresaModel
from .core import Venta

{linea_venta}
"""
with open(os.path.join(models_dir, "lineas.py"), "w", encoding="utf-8") as f: f.write(lineas_content)

pagos_content = f"""from django.db import models
from core.models import EmpresaModel
from .core import Venta, MetodoPago

{pago_venta}
"""
with open(os.path.join(models_dir, "pagos.py"), "w", encoding="utf-8") as f: f.write(pagos_content)

devoluciones_content = f"""from django.db import models
from django.core.exceptions import ValidationError
from core.models import EmpresaModel
from .core import Venta
from .lineas import LineaVenta

{devolucion_venta}


{devolucion_linea}
"""
with open(os.path.join(models_dir, "devoluciones.py"), "w", encoding="utf-8") as f: f.write(devoluciones_content)

init_content = """from .core import EstadoVenta, TipoMetodoPago, SecuenciaVenta, MetodoPago, Venta
from .lineas import LineaVenta
from .pagos import PagoVenta
from .devoluciones import DevolucionVenta, DevolucionLineaVenta

__all__ = [
    "EstadoVenta",
    "TipoMetodoPago",
    "SecuenciaVenta",
    "MetodoPago",
    "Venta",
    "LineaVenta",
    "PagoVenta",
    "DevolucionVenta",
    "DevolucionLineaVenta",
]
"""
with open(os.path.join(models_dir, "__init__.py"), "w", encoding="utf-8") as f: f.write(init_content)

print("Ventas models split completed.")
