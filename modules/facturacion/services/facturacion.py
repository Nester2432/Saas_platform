import logging
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from modules.facturacion.models import Factura, LineaFactura, EstadoFactura
from modules.facturacion.exceptions import FacturaActivaError, FacturaEmitidaError
from modules.ventas.models import Venta, EstadoVenta
from modules.events.event_bus import EventBus
from modules.events import events

logger = logging.getLogger(__name__)

class FacturaService:
    @staticmethod
    @transaction.atomic
    def generar_factura_desde_venta(empresa, venta: Venta, usuario=None) -> Factura:
        """
        Generates a BORRADOR invoice from a confirmed or paid sale.
        """
        # 1. Validation: Sale state
        if venta.estado not in [EstadoVenta.CONFIRMADA, EstadoVenta.PAGADA]:
            raise ValidationError(
                f"No se puede facturar una venta en estado {venta.estado}. Debe estar CONFIRMADA o PAGADA."
            )

        # 2. Validation: Active invoice check
        factura_activa = Factura.objects.filter(
            empresa=empresa,
            venta=venta,
            estado__in=[EstadoFactura.BORRADOR, EstadoFactura.EMITIDA]
        ).exists()
        
        if factura_activa:
            raise FacturaActivaError("La venta ya tiene una factura activa.")

        # 3. Create Factura header
        factura = Factura.objects.create(
            empresa=empresa,
            venta=venta,
            estado=EstadoFactura.BORRADOR,
            subtotal=venta.total, # Assuming total is what we bill for now
            total=venta.total,
            moneda="ARS", # Default
            created_by=usuario
        )

        # 4. Create lines (Snapshot)
        for linea_venta in venta.lineas.all():
            LineaFactura.objects.create(
                empresa=empresa,
                factura=factura,
                producto=linea_venta.producto,
                descripcion=linea_venta.descripcion,
                cantidad=linea_venta.cantidad,
                precio_unitario=linea_venta.precio_unitario,
                subtotal=linea_venta.subtotal
            )

        EventBus.publish(
            "generar_factura_borrador",
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="factura",
            recurso_id=factura.id,
            venta_id=str(venta.id),
            total=float(factura.total)
        )
        return factura

    @staticmethod
    @transaction.atomic
    def emitir_factura(empresa, factura: Factura, punto_venta, usuario=None) -> Factura:
        """
        Finalizes an invoice, assigns a fiscal number, and blocks further edits.
        
        Rules:
        - Invoice must be in BORRADOR status.
        - punto_venta must be provided (and active).
        - Sequence is blocked via select_for_update() to avoid duplicates.
        - Independent sequences per (PuntoVenta, TipoComprobante).
        """
        if factura.estado != EstadoFactura.BORRADOR:
            raise FacturaEmitidaError(
                f"Solo se pueden emitir facturas en estado BORRADOR. "
                f"Estado actual: {factura.estado}"
            )
        
        if factura.punto_venta is not None:
             raise FacturaEmitidaError("La factura ya tiene un punto de venta asignado.")

        if not punto_venta.activo:
            raise ValidationError("El punto de venta seleccionado no está activo.")

        # 1. Lock/Get sequence
        from modules.facturacion.models import SecuenciaComprobante
        secuencia, _ = SecuenciaComprobante.objects.select_for_update().get_or_create(
            empresa=empresa,
            punto_venta=punto_venta,
            tipo_comprobante=factura.tipo,
            defaults={"ultimo_numero": 0}
        )
        
        # 2. Increment and assign
        secuencia.ultimo_numero += 1
        secuencia.save(update_fields=["ultimo_numero", "updated_at"])
        
        factura.punto_venta = punto_venta
        factura.numero_secuencial = secuencia.ultimo_numero
        factura.numero = f"{punto_venta.codigo}-{secuencia.ultimo_numero:08d}"
        
        factura.estado = EstadoFactura.EMITIDA
        factura.fecha_emision = timezone.now()
        factura.updated_by = usuario
        factura.save(update_fields=[
            "punto_venta", "numero_secuencial", "numero", 
            "estado", "fecha_emision", "updated_by", "updated_at"
        ])

        EventBus.publish(
            events.FACTURA_EMITIDA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="factura",
            recurso_id=factura.id,
            numero=factura.numero,
            punto_venta=punto_venta.codigo
        )
        return factura

    @staticmethod
    @transaction.atomic
    def anular_factura(empresa, factura: Factura, usuario=None) -> Factura:
        """
        Voids an invoice.
        """
        if factura.estado == EstadoFactura.ANULADA:
            return factura
            
        factura.estado = EstadoFactura.ANULADA
        factura.updated_by = usuario
        factura.save()
        
        EventBus.publish(
            events.FACTURA_ANULADA,
            empresa_id=empresa.id,
            usuario_id=usuario.id if usuario else None,
            recurso="factura",
            recurso_id=factura.id,
            numero=factura.numero
        )
        return factura
