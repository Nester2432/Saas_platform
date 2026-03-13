import uuid
from datetime import datetime
from django.utils import timezone
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Event Names
VENTA_CREADA = "venta_creada"
VENTA_CONFIRMADA = "venta_confirmada"
VENTA_PAGADA = "venta_pagada"
VENTA_CANCELADA = "venta_cancelada"

FACTURA_EMITIDA = "factura_emitida"
FACTURA_ANULADA = "factura_anulada"

PLAN_CAMBIADO = "plan_cambiado"
SUSCRIPCION_SUSPENDIDA = "suscripcion_suspendida"
SUSCRIPCION_REACTIVADA = "suscripcion_reactivada"
SUBSCRIPTION_CREATED = "subscription_created"
TRIAL_STARTED = "trial_started"
SUBSCRIPTION_CANCELED = "subscription_canceled"
SUBSCRIPTION_UPGRADED = "subscription_upgraded"

PRODUCTO_CREADO = "producto_creado"
STOCK_ACTUALIZADO = "stock_actualizado"

CLIENTE_CREADO = "cliente_creado"

@dataclass(frozen=True)
class Event:
    name: str
    empresa_id: str
    usuario_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    version: str = "1.0"
    timestamp: datetime = field(default_factory=timezone.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_name": self.name,
            "empresa_id": self.empresa_id,
            "usuario_id": self.usuario_id,
            "timestamp": self.timestamp.isoformat(),
            "payload": self.payload
        }
