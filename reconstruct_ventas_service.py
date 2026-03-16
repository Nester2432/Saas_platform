"""
reconstruct_ventas_service.py
Rebuilds a single VentaService class from the mixin files.
"""
import os

BASE = r"c:\Users\Administrador\Desktop\Kevin (despues la borro)\Saas_platform\modules\ventas\services"

# Read all mixin files and extract class bodies
def get_class_body(filepath):
    if not os.path.exists(filepath):
        print(f"NOT FOUND: {filepath}")
        return ""
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    # Find class body after the class definition
    # Each mixin has 'class XXXMixin:' line
    lines = content.split("\n")
    in_class = False
    body_lines = []
    for line in lines:
        if line.startswith("class ") and "Mixin" in line:
            in_class = True
            continue
        if in_class:
            # All lines that are either empty or indented (part of class body)
            if line.startswith("    ") or line == "" or line == "\r":
                body_lines.append(line)
            elif line.strip() == "":
                body_lines.append(line)
            else:
                # Hit a new class or top-level def — stop
                break
    return "\n".join(body_lines)

# Get the header (imports + _TRANSICIONES_VALIDAS) from orquestador_mixin.py
orquestador_file = os.path.join(BASE, "orquestador_mixin.py")
with open(orquestador_file, "r", encoding="utf-8") as f:
    orquestador_content = f.read()

# Extract everything before the class definition
header_end = orquestador_content.find("\nclass VentaOrquestadorMixin")
header = orquestador_content[:header_end]

# Get the body of each mixin
orquestador_body = get_class_body(orquestador_file)
pagos_body = get_class_body(os.path.join(BASE, "pagos_mixin.py"))
devoluciones_body = get_class_body(os.path.join(BASE, "devoluciones_mixin.py"))
validadores_body = get_class_body(os.path.join(BASE, "validadores_mixin.py"))

# Reconstruct a single VentaService class
ventas_service = f"""{header}

_TRANSICIONES_VALIDAS: set[tuple[str, str]] = {{
    (EstadoVenta.BORRADOR,   EstadoVenta.CONFIRMADA),
    (EstadoVenta.BORRADOR,   EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.CANCELADA),
    (EstadoVenta.CONFIRMADA, EstadoVenta.DEVUELTA),
    (EstadoVenta.PAGADA,     EstadoVenta.CANCELADA),
    (EstadoVenta.PAGADA,     EstadoVenta.DEVUELTA),
}}


class VentaService:
    \"\"\"
    Mutation service for Venta lifecycle management.
    
    All methods are static — no instance state, fully thread-safe.
    All public methods are @transaction.atomic.
    \"\"\"

    # ── Public API — creation and editing (BORRADOR phase) ─────────────────
{orquestador_body}

    # ── Public API — payments ─────────────────────────────────────────────
{pagos_body}

    # ── Public API — returns ──────────────────────────────────────────────
{devoluciones_body}

    # ── Private helpers ───────────────────────────────────────────────────
{validadores_body}
"""

output_path = os.path.join(BASE, "ventas.py")
with open(output_path, "w", encoding="utf-8") as f:
    f.write(ventas_service)
print(f"Reconstructed VentaService -> {output_path}")
