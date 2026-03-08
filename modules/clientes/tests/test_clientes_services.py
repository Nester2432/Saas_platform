"""
modules/clientes/tests/test_clientes_services.py

Service-layer tests for the clientes module.

Tests cover ClienteService exclusively — no HTTP, no views.
Each test exercises the service contract:
  - What gets persisted
  - What HistorialCliente events are registered
  - What ValidationErrors are raised
  - Idempotency guarantees (e.g. agregar_etiqueta twice)
  - Atomic rollback behaviour

Strategy:
  - Call service methods directly
  - Assert on DB state (not return values alone)
  - Assert on HistorialCliente to confirm events were registered
"""

from django.test import TestCase
from django.core.exceptions import ValidationError

from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    ClienteEtiqueta,
    NotaCliente,
    HistorialCliente,
)
from modules.clientes.services import ClienteService
from modules.clientes.tests.factories import (
    make_empresa,
    make_usuario,
    make_cliente,
    make_etiqueta,
    make_nota,
)


class CrearClienteServiceTest(TestCase):
    """Tests for ClienteService.crear_cliente."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)

    def test_crear_cliente_basico(self):
        """Service creates and returns a saved Cliente."""
        cliente = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "María", "apellido": "López"},
            usuario=self.usuario,
        )
        self.assertIsNotNone(cliente.id)
        self.assertEqual(cliente.nombre, "María")
        self.assertEqual(cliente.empresa, self.empresa)

    def test_crear_cliente_registra_created_by(self):
        cliente = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Pedro"},
            usuario=self.usuario,
        )
        self.assertEqual(cliente.created_by, self.usuario)
        self.assertEqual(cliente.updated_by, self.usuario)

    def test_crear_cliente_registra_evento_historial(self):
        """Creating a client must automatically register a CREATED event."""
        cliente = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Luis", "email": "luis@test.com"},
            usuario=self.usuario,
        )
        eventos = HistorialCliente.objects.filter(cliente=cliente)
        self.assertEqual(eventos.count(), 1)
        self.assertEqual(eventos.first().tipo_evento, HistorialCliente.TipoEvento.CREATED)

    def test_crear_cliente_sin_email_no_valida_unicidad(self):
        """Creating a client without email must succeed even if others have no email."""
        ClienteService.crear_cliente(self.empresa, {"nombre": "A"})
        # Second client without email should not raise
        ClienteService.crear_cliente(self.empresa, {"nombre": "B"})
        self.assertEqual(Cliente.objects.for_empresa(self.empresa).count(), 2)

    def test_crear_cliente_email_duplicado_lanza_error(self):
        """Duplicate email within the same empresa must raise ValidationError."""
        ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Primero", "email": "dup@test.com"},
        )
        with self.assertRaises(ValidationError) as ctx:
            ClienteService.crear_cliente(
                empresa=self.empresa,
                datos={"nombre": "Segundo", "email": "dup@test.com"},
            )
        self.assertIn("dup@test.com", str(ctx.exception))

    def test_crear_cliente_email_duplicado_rollback(self):
        """On ValidationError, no partial client should be saved."""
        ClienteService.crear_cliente(self.empresa, {"nombre": "A", "email": "dup@test.com"})
        count_antes = Cliente.objects.for_empresa(self.empresa).count()
        with self.assertRaises(ValidationError):
            ClienteService.crear_cliente(self.empresa, {"nombre": "B", "email": "dup@test.com"})
        self.assertEqual(Cliente.objects.for_empresa(self.empresa).count(), count_antes)

    def test_crear_cliente_mismo_email_diferente_empresa(self):
        """The same email is allowed in different empresas."""
        empresa_b = make_empresa()
        ClienteService.crear_cliente(self.empresa, {"nombre": "A", "email": "cross@test.com"})
        cliente_b = ClienteService.crear_cliente(empresa_b, {"nombre": "B", "email": "cross@test.com"})
        self.assertIsNotNone(cliente_b.id)


class ActualizarClienteServiceTest(TestCase):
    """Tests for ClienteService.actualizar_cliente."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)
        self.cliente = make_cliente(self.empresa, nombre="Original", email="orig@test.com")

    def test_actualizar_campo_simple(self):
        ClienteService.actualizar_cliente(
            cliente=self.cliente,
            datos={"nombre": "Modificado"},
            usuario=self.usuario,
        )
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.nombre, "Modificado")

    def test_actualizar_registra_evento_updated(self):
        ClienteService.actualizar_cliente(
            cliente=self.cliente,
            datos={"apellido": "Nuevo"},
            usuario=self.usuario,
        )
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.UPDATED,
        ).first()
        self.assertIsNotNone(evento)
        self.assertIn("apellido", evento.metadata["campos_modificados"])

    def test_actualizar_sin_cambios_no_registra_evento(self):
        """Calling update with identical values must not create a historial event."""
        ClienteService.actualizar_cliente(
            cliente=self.cliente,
            datos={"nombre": self.cliente.nombre},  # same value
            usuario=self.usuario,
        )
        updated_eventos = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.UPDATED,
        )
        self.assertEqual(updated_eventos.count(), 0)

    def test_actualizar_email_a_duplicado_lanza_error(self):
        """Changing email to one already used must raise ValidationError."""
        make_cliente(self.empresa, email="taken@test.com")
        with self.assertRaises(ValidationError):
            ClienteService.actualizar_cliente(
                cliente=self.cliente,
                datos={"email": "taken@test.com"},
            )

    def test_actualizar_email_propio_no_lanza_error(self):
        """Updating to the same email (no real change) must not raise."""
        ClienteService.actualizar_cliente(
            cliente=self.cliente,
            datos={"email": self.cliente.email},
        )
        # No exception raised

    def test_actualizar_registra_updated_by(self):
        ClienteService.actualizar_cliente(
            cliente=self.cliente,
            datos={"nombre": "Cambiado"},
            usuario=self.usuario,
        )
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.updated_by, self.usuario)


class AgregarEtiquetaServiceTest(TestCase):
    """Tests for ClienteService.agregar_etiqueta."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)
        self.cliente = make_cliente(self.empresa)
        self.etiqueta = make_etiqueta(self.empresa, nombre="VIP")

    def test_agregar_etiqueta(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        self.assertIn(self.etiqueta, self.cliente.etiquetas.all())

    def test_agregar_etiqueta_registra_evento(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.TAG_ADDED,
        ).first()
        self.assertIsNotNone(evento)
        self.assertEqual(evento.metadata["etiqueta_nombre"], "VIP")

    def test_agregar_etiqueta_dos_veces_es_idempotente(self):
        """Assigning the same tag twice must not create duplicates."""
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        count = ClienteEtiqueta.objects.filter(
            cliente=self.cliente, etiqueta=self.etiqueta
        ).count()
        self.assertEqual(count, 1)

    def test_agregar_etiqueta_segunda_vez_no_duplica_evento(self):
        """TAG_ADDED event must only be created once for idempotent calls."""
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        count = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.TAG_ADDED,
        ).count()
        self.assertEqual(count, 1)

    def test_agregar_etiqueta_de_otra_empresa_lanza_error(self):
        """Tags from a different empresa must be rejected."""
        empresa_b = make_empresa()
        etiqueta_b = make_etiqueta(empresa_b)
        with self.assertRaises(ValidationError):
            ClienteService.agregar_etiqueta(self.cliente, etiqueta_b)

    def test_quitar_etiqueta(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        ClienteService.quitar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        self.assertNotIn(self.etiqueta, self.cliente.etiquetas.all())

    def test_quitar_etiqueta_registra_evento_removed(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        ClienteService.quitar_etiqueta(self.cliente, self.etiqueta, self.usuario)
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.TAG_REMOVED,
        ).first()
        self.assertIsNotNone(evento)

    def test_quitar_etiqueta_no_asignada_no_falla(self):
        """Removing a tag that was never assigned must succeed silently."""
        # No exception expected
        ClienteService.quitar_etiqueta(self.cliente, self.etiqueta)


class AgregarNotaServiceTest(TestCase):
    """Tests for ClienteService.agregar_nota."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)
        self.cliente = make_cliente(self.empresa)

    def test_agregar_nota_crea_registro(self):
        nota = ClienteService.agregar_nota(
            cliente=self.cliente,
            contenido="Nota de prueba.",
            usuario=self.usuario,
        )
        self.assertIsNotNone(nota.id)
        self.assertEqual(nota.contenido, "Nota de prueba.")
        self.assertEqual(nota.cliente, self.cliente)
        self.assertEqual(nota.empresa, self.empresa)

    def test_agregar_nota_registra_evento(self):
        ClienteService.agregar_nota(self.cliente, "Contenido.", self.usuario)
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.NOTE_ADDED,
        ).first()
        self.assertIsNotNone(evento)

    def test_agregar_nota_registra_created_by(self):
        nota = ClienteService.agregar_nota(self.cliente, "Nota.", self.usuario)
        self.assertEqual(nota.created_by, self.usuario)

    def test_nota_contenido_vacio_lanza_error(self):
        """Empty or whitespace-only content must raise ValidationError."""
        with self.assertRaises(ValidationError):
            ClienteService.agregar_nota(self.cliente, "   ")

    def test_nota_contenido_vacio_string_lanza_error(self):
        with self.assertRaises(ValidationError):
            ClienteService.agregar_nota(self.cliente, "")

    def test_nota_contenido_se_hace_strip(self):
        """Leading/trailing whitespace must be stripped from content."""
        nota = ClienteService.agregar_nota(self.cliente, "  Nota con espacios.  ")
        self.assertEqual(nota.contenido, "Nota con espacios.")

    def test_multiples_notas_en_el_mismo_cliente(self):
        ClienteService.agregar_nota(self.cliente, "Primera.")
        ClienteService.agregar_nota(self.cliente, "Segunda.")
        count = NotaCliente.objects.filter(cliente=self.cliente).count()
        self.assertEqual(count, 2)


class RegistrarEventoServiceTest(TestCase):
    """Tests for ClienteService.registrar_evento."""

    def setUp(self):
        self.empresa = make_empresa()
        self.cliente = make_cliente(self.empresa)

    def test_registrar_evento_manual(self):
        evento = ClienteService.registrar_evento(
            empresa=self.empresa,
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.UPDATED,
            descripcion="Cambio manual.",
            metadata={"motivo": "test"},
        )
        self.assertIsNotNone(evento.id)
        self.assertEqual(evento.tipo_evento, "UPDATED")
        self.assertEqual(evento.metadata["motivo"], "test")

    def test_metadata_default_es_dict_vacio(self):
        evento = ClienteService.registrar_evento(
            empresa=self.empresa,
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
            descripcion="Sin metadata.",
        )
        self.assertEqual(evento.metadata, {})
