"""
modules/clientes/services.py

ClienteService — all business logic for the clientes module.

Rules enforced here (never in views):
- Uniqueness validation (email per empresa)
- Automatic historial event registration on every mutation
- Tenant isolation on every query
- Transactional integrity for multi-step operations

Views call exactly one service method per action.
Services call the ORM directly — never call other services from views.
"""

import logging
from django.db import transaction
from django.core.exceptions import ValidationError

from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    ClienteEtiqueta,
    NotaCliente,
    HistorialCliente,
)

logger = logging.getLogger(__name__)


class ClienteService:

    # ------------------------------------------------------------------
    # Cliente CRUD
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def crear_cliente(empresa, datos: dict, usuario=None, etiqueta_ids: list = None) -> Cliente:
        """
        Create a new Cliente for the given empresa.

        Validates:
        - email uniqueness within the empresa (if email provided)
        - plan limits (billing integration)

        Registers:
        - HistorialCliente(CREATED) automatically
        - HistorialCliente(TAG_ADDED) for each initial tag

        Args:
            empresa:      Empresa instance (tenant)
            datos:        dict with validated field values
            usuario:      Usuario who is creating (for audit trail)
            etiqueta_ids: list of UUIDs for initial tags
        """
        email = datos.get("email", "").strip()

        if email:
            ClienteService._validar_email_unico(empresa, email)

        from modules.billing.services.billing_service import BillingService
        BillingService.check_plan_limits(empresa, "clientes")

        cliente = Cliente(
            empresa=empresa,
            created_by=usuario,
            updated_by=usuario,
            **datos,
        )
        cliente.full_clean()
        cliente.save()

        # Handle initial tags
        if etiqueta_ids:
            etiquetas = EtiquetaCliente.objects.for_empresa(empresa).filter(id__in=etiqueta_ids)
            for etiqueta in etiquetas:
                ClienteService.agregar_etiqueta(cliente, etiqueta, usuario)

        ClienteService.registrar_evento(
            empresa=empresa,
            cliente=cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
            descripcion=f"Cliente '{cliente.nombre_completo}' creado.",
            metadata={"email": email, "telefono": datos.get("telefono", "")},
            usuario=usuario,
        )

        from modules.events.event_bus import EventBus
        from modules.events import events

        EventBus.publish(
            events.CLIENTE_CREADO,
            empresa_id=str(empresa.id),
            usuario_id=str(usuario.id) if usuario else None,
            cliente_id=str(cliente.id)
        )

        logger.info(
            "Cliente created id=%s empresa=%s by user=%s",
            cliente.id, empresa.id, getattr(usuario, "id", None)
        )
        return cliente

    @staticmethod
    @transaction.atomic
    def actualizar_cliente(cliente: Cliente, datos: dict, usuario=None) -> Cliente:
        """
        Update an existing Cliente's fields.

        Validates:
        - email uniqueness within the empresa (if email is being changed)

        Registers:
        - HistorialCliente(UPDATED) with list of changed fields

        Args:
            cliente:  Cliente instance to update
            datos:    dict with fields to update (partial update supported)
            usuario:  Usuario performing the update

        Returns:
            Updated Cliente instance

        Raises:
            ValidationError if new email already exists for this empresa
        """
        nuevo_email = datos.get("email", "").strip()
        if nuevo_email and nuevo_email != cliente.email:
            ClienteService._validar_email_unico(
                cliente.empresa, nuevo_email, excluir_id=cliente.id
            )

        campos_modificados = []
        for campo, valor in datos.items():
            if getattr(cliente, campo, None) != valor:
                campos_modificados.append(campo)
                setattr(cliente, campo, valor)

        if campos_modificados:
            cliente.updated_by = usuario
            cliente.full_clean()
            cliente.save(update_fields=campos_modificados + ["updated_by", "updated_at"])

            ClienteService.registrar_evento(
                empresa=cliente.empresa,
                cliente=cliente,
                tipo_evento=HistorialCliente.TipoEvento.UPDATED,
                descripcion=f"Cliente actualizado. Campos: {', '.join(campos_modificados)}.",
                metadata={"campos_modificados": campos_modificados},
                usuario=usuario,
            )

            logger.info(
                "Cliente updated id=%s fields=%s empresa=%s by user=%s",
                cliente.id, campos_modificados,
                cliente.empresa_id, getattr(usuario, "id", None)
            )

        return cliente

    # ------------------------------------------------------------------
    # Etiquetas
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def agregar_etiqueta(cliente: Cliente, etiqueta: EtiquetaCliente, usuario=None) -> ClienteEtiqueta:
        """
        Attach a tag to a client.

        Validates:
        - Both belong to the same empresa (cross-tenant guard)
        - Tag is not already assigned (idempotent — returns existing if found)

        Registers:
        - HistorialCliente(TAG_ADDED)
        """
        if cliente.empresa_id != etiqueta.empresa_id:
            raise ValidationError(
                "La etiqueta no pertenece a la misma empresa que el cliente."
            )

        cliente_etiqueta, created = ClienteEtiqueta.objects.get_or_create(
            empresa=cliente.empresa,
            cliente=cliente,
            etiqueta=etiqueta,
            defaults={"created_by": usuario, "updated_by": usuario},
        )

        if created:
            ClienteService.registrar_evento(
                empresa=cliente.empresa,
                cliente=cliente,
                tipo_evento=HistorialCliente.TipoEvento.TAG_ADDED,
                descripcion=f"Etiqueta '{etiqueta.nombre}' agregada.",
                metadata={"etiqueta_id": str(etiqueta.id), "etiqueta_nombre": etiqueta.nombre},
                usuario=usuario,
            )

        return cliente_etiqueta

    @staticmethod
    @transaction.atomic
    def quitar_etiqueta(cliente: Cliente, etiqueta: EtiquetaCliente, usuario=None) -> None:
        """
        Remove a tag from a client.

        Silently succeeds if the tag was not assigned.
        Registers HistorialCliente(TAG_REMOVED) only if the tag existed.
        """
        deleted_count, _ = ClienteEtiqueta.objects.filter(
            empresa=cliente.empresa,
            cliente=cliente,
            etiqueta=etiqueta,
        ).hard_delete()

        if deleted_count > 0:
            ClienteService.registrar_evento(
                empresa=cliente.empresa,
                cliente=cliente,
                tipo_evento=HistorialCliente.TipoEvento.TAG_REMOVED,
                descripcion=f"Etiqueta '{etiqueta.nombre}' removida.",
                metadata={"etiqueta_id": str(etiqueta.id), "etiqueta_nombre": etiqueta.nombre},
                usuario=usuario,
            )

    # ------------------------------------------------------------------
    # Notas
    # ------------------------------------------------------------------

    @staticmethod
    @transaction.atomic
    def agregar_nota(cliente: Cliente, contenido: str, usuario=None) -> NotaCliente:
        """
        Add a note to a client record.

        Args:
            cliente:   Target Cliente
            contenido: Note text (must be non-empty)
            usuario:   Author of the note

        Returns:
            NotaCliente instance

        Raises:
            ValidationError if contenido is empty
        """
        contenido = contenido.strip()
        if not contenido:
            raise ValidationError("El contenido de la nota no puede estar vacío.")

        nota = NotaCliente.objects.create(
            empresa=cliente.empresa,
            cliente=cliente,
            contenido=contenido,
            created_by=usuario,
            updated_by=usuario,
        )

        ClienteService.registrar_evento(
            empresa=cliente.empresa,
            cliente=cliente,
            tipo_evento=HistorialCliente.TipoEvento.NOTE_ADDED,
            descripcion="Nota agregada.",
            metadata={"nota_id": str(nota.id), "preview": contenido[:100]},
            usuario=usuario,
        )

        return nota

    # ------------------------------------------------------------------
    # Historial
    # ------------------------------------------------------------------

    @staticmethod
    def registrar_evento(
        empresa,
        cliente: Cliente,
        tipo_evento: str,
        descripcion: str,
        metadata: dict = None,
        usuario=None,
    ) -> HistorialCliente:
        """
        Append an immutable event to the client's history.

        This is the ONLY way to write to HistorialCliente.
        Called internally by all other ClienteService methods.
        Can also be called directly by external modules for cross-module events
        (e.g. a sale is linked to a client → ventas module records a SALE_CREATED event).

        Args:
            empresa:      Tenant
            cliente:      The client this event belongs to
            tipo_evento:  One of HistorialCliente.TipoEvento choices
            descripcion:  Human-readable summary
            metadata:     Arbitrary dict with event context
            usuario:      Who triggered the event

        Returns:
            HistorialCliente instance
        """
        return HistorialCliente.objects.create(
            empresa=empresa,
            cliente=cliente,
            tipo_evento=tipo_evento,
            descripcion=descripcion,
            metadata=metadata or {},
            created_by=usuario,
            updated_by=usuario,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validar_email_unico(empresa, email: str, excluir_id=None):
        """
        Check that email is not already used by another active client in this empresa.

        Args:
            empresa:     Tenant scope
            email:       Email to check
            excluir_id:  UUID of the client to exclude (for updates)

        Raises:
            ValidationError if duplicate found
        """
        qs = Cliente.objects.for_empresa(empresa).filter(email=email)
        if excluir_id:
            qs = qs.exclude(id=excluir_id)

        if qs.exists():
            raise ValidationError(
                f"Ya existe un cliente con el email '{email}' en esta empresa."
            )
