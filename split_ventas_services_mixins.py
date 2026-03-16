import os

file_path = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\services\ventas.py"
services_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\services"

with open(file_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

def extract_methods(method_names):
    extracted = []
    in_method = False
    current_method = []
    
    for i, line in enumerate(lines):
        # Look for start of method
        if line.startswith("    @staticmethod"):
            # check if the next lines define one of our methods
            # Next line might be @transaction.atomic or def
            target_method = None
            for j in range(i, min(i+5, len(lines))):
                for name in method_names:
                    if f"def {name}(" in lines[j]:
                        target_method = name
                        break
                if target_method: break
            
            if target_method:
                in_method = True
                current_method = []
        
        if in_method:
            # check for end of method. End of method is when we hit another "    @staticmethod" or the end of the class.
            if len(current_method) > 0 and line.startswith("    @staticmethod"):
                in_method = False
                extracted.append("".join(current_method))
                current_method = []
                # Don't skip this line, it might be the start of another!
                # But wait, Python's for loop won't go back. Instead of reading line by line iteratively, just match blocks by index.
                pass
                
    return extracted

def get_block(start_str, next_strs):
    start_idx = -1
    for i, l in enumerate(lines):
        if start_str in l:
            start_idx = i
            break
    if start_idx == -1: return ""
    
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if any(ns in lines[i] for ns in next_strs):
            end_idx = i
            break
            
    return "".join(lines[start_idx:end_idx])

# Since VentaService methods are static, we can extract them precisely.
# We'll just read everything manually using string splits.

with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# We can split content by "    @staticmethod"
parts = content.split("\n    @staticmethod\n")

if len(parts) == 1:
    print("Failed to split by @staticmethod")
else:
    header = parts[0]
    methods = parts[1:]
    
    # Prefix each method with @staticmethod since it was absorbed by the split
    methods = ["    @staticmethod\n" + m for m in methods]
    
    orquestador_methods = []
    pagos_methods = []
    devoluciones_methods = []
    validadores_methods = []
    
    for m in methods:
        if "def crear_venta" in m or "def agregar_linea" in m or "def quitar_linea" in m or "def confirmar_venta" in m or "def cancelar_venta" in m:
            m = m.replace("VentaService.", "VentaOrquestador.")
            orquestador_methods.append(m)
        elif "def registrar_pago" in m or "def marcar_como_pagada" in m:
            m = m.replace("VentaService.", "PagoVentaService.")
            pagos_methods.append(m)
        elif "def registrar_devolucion" in m:
            m = m.replace("VentaService.", "DevolucionVentaService.")
            devoluciones_methods.append(m)
        else: # validadores and helpers
            # We must make validadores available to everyone, so we'll put them in VentaValidadores
            m = m.replace("VentaService.", "VentaValidadores.")
            validadores_methods.append(m)
            
    # Now wait, Orquestador still needs access to VentaValidadores, PagoVentaService needs access to VentaOrquestador...
    # Too much coupling. I'll just keep them all in one class `VentaService` but spread across files via mixins!
    
    # MIXINS approach!
    
    mixin_validadores = "class VentaValidadoresMixin:\n" + "".join(validadores_methods)
    mixin_orquestador = "class VentaOrquestadorMixin:\n" + "".join(orquestador_methods).replace("VentaOrquestador.", "VentaService.")
    mixin_pagos = "class PagoVentaMixin:\n" + "".join(pagos_methods).replace("PagoVentaService.", "VentaService.")
    mixin_devoluciones = "class DevolucionVentaMixin:\n" + "".join(devoluciones_methods).replace("DevolucionVentaService.", "VentaService.")
    
    with open("modules/ventas/services/orquestador_mixin.py", "w", encoding="utf-8") as f:
        f.write(header + "\n" + mixin_orquestador)
        
    with open("modules/ventas/services/pagos_mixin.py", "w", encoding="utf-8") as f:
        f.write(header + "\n" + mixin_pagos)
        
    with open("modules/ventas/services/devoluciones_mixin.py", "w", encoding="utf-8") as f:
        f.write(header + "\n" + mixin_devoluciones)
        
    with open("modules/ventas/services/validadores_mixin.py", "w", encoding="utf-8") as f:
        f.write(header + "\n" + mixin_validadores)
        
    # Reconstruct VentaService in __init__.py (or ventas.py)
    merged_service = f"""
from .orquestador_mixin import VentaOrquestadorMixin
from .pagos_mixin import PagoVentaMixin
from .devoluciones_mixin import DevolucionVentaMixin
from .validadores_mixin import VentaValidadoresMixin

class VentaService(
    VentaOrquestadorMixin,
    PagoVentaMixin,
    DevolucionVentaMixin,
    VentaValidadoresMixin
):
    pass
"""
    with open("modules/ventas/services/ventas.py", "w", encoding="utf-8") as f:
        f.write(header + "\n" + merged_service)
    
    print("Ventas Services Mixins successfully created!")
