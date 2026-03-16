import os
import re

file_path = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\services\ventas.py"
services_dir = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\services"

with open(file_path, "r", encoding="utf-8") as f:
    text = f.read()

def get_block(method_name, is_last=False):
    # Match from def method_name (or @staticmethod def method_name) until the next @staticmethod
    pattern = r'(    @staticmethod\s+@transaction\.atomic\s+def ' + method_name + r'.*?)(?=    @staticmethod|\Z)'
    if "_validar" in method_name or "_siguiente" in method_name or "_recalcular" in method_name or "_es_devolucion" in method_name or "_snapshot" in method_name:
         pattern = r'(    @staticmethod\s+def ' + method_name + r'.*?)(?=    @staticmethod|\Z)'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        print(f"FAILED: {method_name}")
        return ""
    code = match.group(1).strip()
    # Unindent everything 4 spaces since we might put it in a module level or a new class
    lines = code.split("\n")
    unindented = [line[4:] if line.startswith("    ") else line for line in lines]
    return "\n".join(unindented)

# We will just split the file but KEEP THEM in classes to avoid changing the caller's code
# Or better, we rename VentaService calls in codebase?
# A safer approach: keeping a VentaService facade in __init__.py

imports = """import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from modules.inventario.services import MovimientoService
from modules.billing.services.billing_service import BillingService
from modules.ventas.exceptions import (
    TransicionVentaInvalidaError,
    VentaSinLineasError,
    PagoInsuficienteError,
    DevolucionInvalidaError,
)
from modules.ventas.models import (
    DevolucionLineaVenta,
    DevolucionVenta,
    EstadoVenta,
    LineaVenta,
    PagoVenta,
    SecuenciaVenta,
    Venta,
)
from modules.events.event_bus import EventBus
from modules.events import events

logger = logging.getLogger(__name__)

_TRANSICIONES_VALIDAS: set[tuple[str, str]] = {
    (EstadoVenta.BORRADOR,   EstadoVenta.CONFIRMADA),
    (EstadoVenta.BORRADOR,   EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.DEVUELTA),
    (EstadoVenta.PAGADA,     EstadoVenta.CANCELADA),
    (EstadoVenta.PAGADA,     EstadoVenta.DEVUELTA),
}
"""

crear_venta = get_block("crear_venta")
agregar_linea = get_block("agregar_linea")
quitar_linea = get_block("quitar_linea")
confirmar_venta = get_block("confirmar_venta")
registrar_pago = get_block("registrar_pago")
cancelar_venta = get_block("cancelar_venta")
registrar_devolucion = get_block("registrar_devolucion")
marcar_como_pagada = get_block("marcar_como_pagada")

_siguiente_numero = get_block("_siguiente_numero")
_validar_transicion = get_block("_validar_transicion")
_validar_editable = get_block("_validar_editable")
_validar_tenant_venta = get_block("_validar_tenant_venta")
_validar_tenant_cliente = get_block("_validar_tenant_cliente")
_validar_tenant_turno = get_block("_validar_tenant_turno")
_validar_tenant_producto = get_block("_validar_tenant_producto")
_recalcular_totales = get_block("_recalcular_totales")
_validar_items_devolucion = get_block("_validar_items_devolucion")
_es_devolucion_total = get_block("_es_devolucion_total")
_snapshot_cliente = get_block("_snapshot_cliente")

# Create validadores.py
with open(os.path.join(services_dir, "_validadores.py"), "w", encoding="utf-8") as f:
    f.write(imports + f"""
class VentaValidadores:
    {_siguiente_numero.replace("VentaService", "VentaValidadores", 10)}

    {_validar_transicion.replace("VentaService", "VentaValidadores", 10)}

    {_validar_editable.replace("VentaService", "VentaValidadores", 10)}

    {_validar_tenant_venta.replace("VentaService", "VentaValidadores", 10)}

    {_validar_tenant_cliente.replace("VentaService", "VentaValidadores", 10)}

    {_validar_tenant_turno.replace("VentaService", "VentaValidadores", 10)}

    {_validar_tenant_producto.replace("VentaService", "VentaValidadores", 10)}

    {_recalcular_totales.replace("VentaService", "VentaValidadores", 10)}

    {_validar_items_devolucion.replace("VentaService", "VentaValidadores", 10)}

    {_es_devolucion_total.replace("VentaService", "VentaValidadores", 10)}

    {_snapshot_cliente.replace("VentaService", "VentaValidadores", 10)}
    """.replace("\n@", "\n    @"))

# For the rest, we write them as module level functions
# But it's easier to keep them in one class and just replace references.
# Wait, replacing across strings is messy. I will just split `modules/ventas/services/ventas.py` manually with replace_file_content! 
# No, 1000 lines is impossible for replace_file_content.
# I will use a simple regex replace across the whole Saas_platform to rename VentaService to specific services.
