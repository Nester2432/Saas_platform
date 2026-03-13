import logging

logger = logging.getLogger(__name__)

class EmailService:
    @staticmethod
    def send_email(to_email: str, subject: str, body: str):
        """Mocked Email sending."""
        # Simulated logic that could raise exception if an external API fails
        logger.info(f"[EMAIL MOCK] To: {to_email} | Subject: {subject} | Body length: {len(body)}")
