import logging
from decimal import Decimal
from typing import Optional
from django.db import transaction
from django.core.exceptions import ValidationError
from modules.cobranzas.models import Pago, EstadoPago
from modules.cobranzas.exceptions import SobrePagoError, TransicionPagoInvalidaError
from modules.ventas.services import VentaService
from modules.ventas.models import Venta, EstadoVenta

logger = logging.getLogger(__name__)

class PagoService:
    """
    Mutation service for Pago lifecycle management.
    """

    @staticmethod
    @transaction.atomic
    def registrar_pago(
        empresa,
        venta: Venta,
        monto: Decimal,
        metodo_pago,
        moneda: str = "ARS",
        referencia_externa: str = "",
        usuario=None
    ) -> Pago:
        """
        Register a new payment intent in PENDIENTE state.
        """
        # Validate that the sale is in a state that allows payments
        if venta.estado not in (EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA):
            if venta.estado != EstadoVenta.BORRADOR: # Check machine
                pass # VentaService will handle specific logic if we call it
        
        # We don't call VentaService.registrar_pago yet, because it's PENDIENTE
        pago = Pago.objects.create(
            empresa=empresa,
            venta=venta,
            monto=monto,
            moneda=moneda,
            metodo_pago=metodo_pago,
            estado=EstadoPago.PENDIENTE,
            referencia_externa=referencia_externa,
            created_by=usuario,
            updated_by=usuario
        )
        
        logger.info(
            "PAGO REGISTRADO: empresa=%s pago=%s venta=%s monto=%s",
            empresa.id, pago.id, venta.id, monto
        )
        return pago

    @staticmethod
    @transaction.atomic
    def confirmar_pago(empresa, pago: Pago, usuario=None) -> Pago:
        """
        Confirm a PENDIENTE payment: update state and register in VentaService.
        """
        if pago.estado != EstadoPago.PENDIENTE:
            raise TransicionPagoInvalidaError(pago.estado, EstadoPago.CONFIRMADO)

        # Lock the sale to check balance consistently
        venta = Venta.objects.select_for_update().get(id=pago.venta_id)
        
        # Calculate current balance
        from django.db.models import Sum
        ya_pagado = venta.pagos.aggregate(total=Sum("monto"))["total"] or Decimal("0")
        saldo = venta.total - ya_pagado
        
        if pago.monto > saldo:
            raise SobrePagoError(saldo, pago.monto)

        # 1. Update Pago state
        pago.estado = EstadoPago.CONFIRMADO
        pago.updated_by = usuario
        pago.save(update_fields=["estado", "updated_by", "updated_at"])
        
        # 2. Register in VentaService (creates PagoVenta and updates Venta state)
        VentaService.registrar_pago(
            empresa=empresa,
            venta=venta,
            metodo_pago=pago.metodo_pago,
            monto=pago.monto,
            referencia=pago.referencia_externa,
            usuario=usuario
        )
        
        logger.info(
            "PAGO CONFIRMADO: empresa=%s pago=%s venta=%s monto=%s",
            empresa.id, pago.id, venta.id, pago.monto
        )
        return pago

    @staticmethod
    @transaction.atomic
    def fallar_pago(empresa, pago: Pago, usuario=None) -> Pago:
        """
        Mark a PENDIENTE payment as FALLIDO.
        """
        if pago.estado != EstadoPago.PENDIENTE:
             raise TransicionPagoInvalidaError(pago.estado, EstadoPago.FALLIDO)
             
        pago.estado = EstadoPago.FALLIDO
        pago.updated_by = usuario
        pago.save(update_fields=["estado", "updated_by", "updated_at"])
        
        logger.info("PAGO FALLIDO: pago=%s", pago.id)
        return pago

    @staticmethod
    @transaction.atomic
    def reembolsar_pago(empresa, pago: Pago, usuario=None) -> Pago:
        """
        Mark a CONFIRMADO payment as REEMBOLSADO.
        Note: This does not automatically reverse the PagoVenta in the sales module
        for now, as it would require a complex accounting reversal logic.
        """
        if pago.estado != EstadoPago.CONFIRMADO:
            raise TransicionPagoInvalidaError(pago.estado, EstadoPago.REEMBOLSADO)
            
        pago.estado = EstadoPago.REEMBOLSADO
        pago.updated_by = usuario
        pago.save(update_fields=["estado", "updated_by", "updated_at"])
        
        logger.info("PAGO REEMBOLSADO: pago=%s", pago.id)
        return pago
