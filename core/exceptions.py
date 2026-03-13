"""
core/exceptions.py

Custom DRF exception handler.

Ensures all API errors return a consistent JSON shape:
{
    "error": true,
    "code": "validation_error",
    "message": "Human-readable message",
    "details": { ... }  # field errors or extra context
}
"""

import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import exceptions

def custom_exception_handler(exc, context):
    """
    Custom exception handler that wraps DRF errors in a consistent envelope.
    """
    if isinstance(exc, DjangoValidationError):
        detail = exc.message_dict if hasattr(exc, 'message_dict') else exc.messages
        exc = exceptions.ValidationError(detail=detail)

    response = exception_handler(exc, context)

    if response is not None:
        error_data = {
            "error": True,
            "code": _get_error_code(response.status_code),
            "message": _get_message(response.data),
            "details": response.data,
        }
        response.data = error_data

    else:
        # Unhandled exception — log it and return 500
        logger.exception(
            "Unhandled exception in %s",
            context.get("view", "unknown view"),
            exc_info=exc,
        )
        response = Response(
            {
                "error": True,
                "code": "internal_error",
                "message": "Ocurrió un error interno. Por favor intente más tarde.",
                "details": {},
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return response


def _get_error_code(status_code):
    codes = {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        422: "validation_error",
        429: "too_many_requests",
        500: "internal_error",
    }
    return codes.get(status_code, "error")


def _get_message(data):
    if isinstance(data, dict):
        if "detail" in data:
            return str(data["detail"])
        if "non_field_errors" in data:
            errors = data["non_field_errors"]
            return str(errors[0]) if errors else "Error de validación."
    if isinstance(data, list) and data:
        return str(data[0])
    return "Se produjo un error en la solicitud."
