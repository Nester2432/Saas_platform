import contextvars
from uuid import UUID
from typing import Optional

# To be used for storing the current empresa_id in a thread-local/task-local way.
_current_empresa_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_empresa_id", default=None
)

def set_current_empresa(empresa_id: Optional[str]) -> contextvars.Token:
    """
    Sets the current empresa_id in the context.
    Returns a token that can be used to reset the context later.
    """
    if isinstance(empresa_id, UUID):
        empresa_id = str(empresa_id)
    return _current_empresa_id.set(empresa_id)

def get_current_empresa() -> Optional[str]:
    """
    Retrieves the current empresa_id from the context.
    """
    return _current_empresa_id.get()

def clear_current_empresa() -> None:
    """
    Explicitly clears the tenant context.
    """
    _current_empresa_id.set(None)

def reset_current_empresa(token: contextvars.Token) -> None:
    """
    Resets the empresa_id in the context using the provided token.
    """
    _current_empresa_id.reset(token)
