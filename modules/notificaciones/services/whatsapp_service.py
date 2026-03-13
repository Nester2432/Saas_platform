import logging

logger = logging.getLogger(__name__)

class WhatsAppService:
    @staticmethod
    def send_message(telefono: str, mensaje: str):
        """Mocked WhatsApp sending."""
        # Simulated logic that could raise exception if an external API fails
        logger.info(f"[WHATSAPP MOCK] To: {telefono} | Message: {mensaje}")
