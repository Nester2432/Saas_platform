import logging
from .email_service import EmailService
from .whatsapp_service import WhatsAppService
from modules.clientes.models import Cliente
from modules.ventas.models import Venta
from modules.facturacion.models import Factura

logger = logging.getLogger(__name__)

class NotificacionService:
    """Facade for sending notifications across different channels."""

    @staticmethod
    def enviar_confirmacion_venta(empresa_id: str, venta_id: str):
        venta = Venta.objects.select_related("cliente").get(id=venta_id, empresa_id=empresa_id)
        if not venta.cliente:
            logger.warning(f"Venta {venta_id} tiene cliente nulo. No se enviará confirmación.")
            return

        cliente = venta.cliente
        
        # Intentar email
        if cliente.email:
            EmailService.send_email(
                to_email=cliente.email,
                subject=f"Confirmación de tu compra #{venta.numero}",
                body=f"Hola {cliente.nombre}, gracias por tu compra por un total de {venta.total}."
            )
        
        # Intentar WhatsApp si no hay email (o ambas, depende de la regla de negocio)
        if cliente.telefono:
            WhatsAppService.send_message(
                telefono=cliente.telefono,
                mensaje=f"Hola {cliente.nombre}, tu compra #{venta.numero} fue confirmada. Gracias!"
            )

    @staticmethod
    def enviar_bienvenida_cliente(empresa_id: str, cliente_id: str):
        cliente = Cliente.objects.get(id=cliente_id, empresa_id=empresa_id)
        
        if cliente.email:
            EmailService.send_email(
                to_email=cliente.email,
                subject="¡Bienvenido!",
                body=f"Hola {cliente.nombre}, bienvenido a nuestra plataforma."
            )
        
        if cliente.telefono:
            WhatsAppService.send_message(
                telefono=cliente.telefono,
                mensaje=f"¡Hola {cliente.nombre}! Bienvenido a nuestro servicio."
            )

    @staticmethod
    def enviar_factura(empresa_id: str, factura_id: str):
        factura = Factura.objects.select_related("venta__cliente").get(id=factura_id, empresa_id=empresa_id)
        cliente = factura.venta.cliente if factura.venta and factura.venta.cliente else None

        if not cliente:
            logger.warning(f"Factura {factura_id} no tiene cliente asociado. No se enviará notificación.")
            return

        if cliente.email:
            EmailService.send_email(
                to_email=cliente.email,
                subject=f"Tu factura {factura.numero} está lista",
                body=f"Hola {cliente.nombre}, adjuntamos (simulado) la factura {factura.numero} por {factura.total}."
            )
            
        if cliente.telefono:
            WhatsAppService.send_message(
                telefono=cliente.telefono,
                mensaje=f"Hola {cliente.nombre}, se ha emitido la factura {factura.numero} por tu compra."
            )
