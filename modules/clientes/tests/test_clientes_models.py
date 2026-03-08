"""
modules/clientes/tests/test_clientes_models.py

Model-level tests for the clientes module.

Tests cover:
- Basic instantiation and field defaults
- email uniqueness constraint per empresa (not global)
- Soft delete behaviour (deleted_at, is_deleted, manager exclusion)
- HistorialCliente immutability guard
- EtiquetaCliente scoping
- ClienteEtiqueta cross-tenant validation

These tests hit the database directly — no HTTP, no services.
They validate that our model layer is correct independently of views/services.
"""

from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    ClienteEtiqueta,
    NotaCliente,
    HistorialCliente,
)
from modules.clientes.tests.factories import (
    make_empresa,
    make_usuario,
    make_cliente,
    make_etiqueta,
)


class ClienteModelTest(TestCase):
    """Tests for the Cliente model."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)

    # ------------------------------------------------------------------
    # Creation & defaults
    # ------------------------------------------------------------------

    def test_crear_cliente_minimo(self):
        """A client can be created with only nombre."""
        cliente = Cliente.objects.create(
            empresa=self.empresa,
            nombre="Juan",
        )
        self.assertIsNotNone(cliente.id)
        self.assertEqual(cliente.nombre, "Juan")
        self.assertEqual(cliente.apellido, "")
        self.assertEqual(cliente.email, "")
        self.assertTrue(cliente.activo)
        self.assertIsNone(cliente.deleted_at)
        self.assertFalse(cliente.is_deleted)

    def test_nombre_completo_con_apellido(self):
        cliente = make_cliente(self.empresa, nombre="Ana", apellido="García")
        self.assertEqual(cliente.nombre_completo, "Ana García")

    def test_nombre_completo_sin_apellido(self):
        cliente = make_cliente(self.empresa, nombre="Ana", apellido="")
        self.assertEqual(cliente.nombre_completo, "Ana")

    def test_uuid_pk_asignado(self):
        cliente = make_cliente(self.empresa)
        self.assertIsNotNone(cliente.id)
        # id must be a UUID — str representation has 36 chars (with hyphens)
        self.assertEqual(len(str(cliente.id)), 36)

    def test_metadata_default_es_dict_vacio(self):
        cliente = make_cliente(self.empresa)
        self.assertEqual(cliente.metadata, {})

    def test_timestamps_auto_asignados(self):
        cliente = make_cliente(self.empresa)
        self.assertIsNotNone(cliente.created_at)
        self.assertIsNotNone(cliente.updated_at)

    # ------------------------------------------------------------------
    # Email uniqueness per empresa
    # ------------------------------------------------------------------

    def test_email_unico_dentro_empresa(self):
        """Two clients in the same empresa cannot share an email."""
        make_cliente(self.empresa, email="dup@test.com")
        with self.assertRaises(IntegrityError):
            # Bypass service to test the DB constraint directly
            Cliente.objects.create(
                empresa=self.empresa,
                nombre="Otro",
                email="dup@test.com",
            )

    def test_email_puede_repetirse_entre_empresas(self):
        """The same email IS allowed across different empresas."""
        empresa_b = make_empresa()
        make_cliente(self.empresa, email="shared@test.com")
        # Must not raise
        cliente_b = make_cliente(empresa_b, email="shared@test.com")
        self.assertIsNotNone(cliente_b.id)

    def test_email_vacio_no_dispara_constraint(self):
        """Multiple clients with empty email are allowed in the same empresa."""
        make_cliente(self.empresa, email="")
        # Must not raise — the constraint has condition email__gt=""
        cliente_b = make_cliente(self.empresa, email="")
        self.assertIsNotNone(cliente_b.id)

    def test_email_soft_deleted_no_bloquea_nuevo(self):
        """
        A soft-deleted client's email should NOT block a new client
        with the same email in the same empresa.
        The UniqueConstraint has condition deleted_at__isnull=True.
        """
        cliente_viejo = make_cliente(self.empresa, email="recycle@test.com")
        cliente_viejo.soft_delete()

        # Now creating another client with the same email should succeed
        nuevo = Cliente.objects.create(
            empresa=self.empresa,
            nombre="Nuevo",
            email="recycle@test.com",
        )
        self.assertIsNotNone(nuevo.id)

    # ------------------------------------------------------------------
    # Soft delete
    # ------------------------------------------------------------------

    def test_soft_delete_marca_deleted_at(self):
        cliente = make_cliente(self.empresa)
        self.assertIsNone(cliente.deleted_at)
        cliente.soft_delete()
        self.assertIsNotNone(cliente.deleted_at)

    def test_soft_delete_is_deleted_property(self):
        cliente = make_cliente(self.empresa)
        self.assertFalse(cliente.is_deleted)
        cliente.soft_delete()
        self.assertTrue(cliente.is_deleted)

    def test_soft_deleted_excluido_del_manager_default(self):
        """Manager .objects.all() must not return soft-deleted clients."""
        cliente = make_cliente(self.empresa)
        cliente.soft_delete()
        ids = Cliente.objects.for_empresa(self.empresa).values_list("id", flat=True)
        self.assertNotIn(cliente.id, ids)

    def test_with_deleted_incluye_soft_deleted(self):
        """Manager .with_deleted() must include soft-deleted clients."""
        cliente = make_cliente(self.empresa)
        cliente.soft_delete()
        ids = Cliente.objects.with_deleted().for_empresa(self.empresa).values_list("id", flat=True)
        self.assertIn(cliente.id, ids)

    def test_restore_limpia_deleted_at(self):
        cliente = make_cliente(self.empresa)
        cliente.soft_delete()
        self.assertTrue(cliente.is_deleted)
        cliente.restore()
        self.assertFalse(cliente.is_deleted)
        self.assertIsNone(cliente.deleted_at)

    def test_soft_delete_registra_updated_by(self):
        cliente = make_cliente(self.empresa)
        cliente.soft_delete(user=self.usuario)
        cliente.refresh_from_db()
        self.assertEqual(cliente.updated_by, self.usuario)

    # ------------------------------------------------------------------
    # Ordering and indexing
    # ------------------------------------------------------------------

    def test_ordering_por_defecto_apellido_nombre(self):
        """Default queryset ordering is apellido, nombre."""
        make_cliente(self.empresa, nombre="Zara", apellido="Zeta")
        make_cliente(self.empresa, nombre="Ana", apellido="Alfa")
        clientes = list(Cliente.objects.for_empresa(self.empresa))
        self.assertEqual(clientes[0].apellido, "Alfa")
        self.assertEqual(clientes[1].apellido, "Zeta")


class EtiquetaClienteModelTest(TestCase):
    """Tests for the EtiquetaCliente model."""

    def setUp(self):
        self.empresa = make_empresa()

    def test_crear_etiqueta(self):
        etiqueta = make_etiqueta(self.empresa, nombre="VIP", color="#FFD700")
        self.assertEqual(etiqueta.nombre, "VIP")
        self.assertEqual(etiqueta.color, "#FFD700")

    def test_nombre_unico_por_empresa(self):
        """Two tags with the same name in the same empresa should fail."""
        make_etiqueta(self.empresa, nombre="Premium")
        with self.assertRaises(IntegrityError):
            EtiquetaCliente.objects.create(
                empresa=self.empresa,
                nombre="Premium",
                color="#000000",
            )

    def test_nombre_puede_repetirse_entre_empresas(self):
        empresa_b = make_empresa()
        make_etiqueta(self.empresa, nombre="VIP")
        etiqueta_b = make_etiqueta(empresa_b, nombre="VIP")
        self.assertIsNotNone(etiqueta_b.id)

    def test_soft_delete_etiqueta(self):
        etiqueta = make_etiqueta(self.empresa)
        etiqueta.soft_delete()
        self.assertTrue(etiqueta.is_deleted)
        self.assertNotIn(
            etiqueta.id,
            EtiquetaCliente.objects.for_empresa(self.empresa).values_list("id", flat=True)
        )


class ClienteEtiquetaModelTest(TestCase):
    """Tests for the ClienteEtiqueta through model."""

    def setUp(self):
        self.empresa = make_empresa()
        self.cliente = make_cliente(self.empresa)
        self.etiqueta = make_etiqueta(self.empresa)

    def test_asignar_etiqueta_a_cliente(self):
        ce = ClienteEtiqueta.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            etiqueta=self.etiqueta,
        )
        self.assertEqual(ce.cliente, self.cliente)
        self.assertEqual(ce.etiqueta, self.etiqueta)

    def test_etiqueta_duplicada_en_mismo_cliente_falla(self):
        ClienteEtiqueta.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            etiqueta=self.etiqueta,
        )
        with self.assertRaises(IntegrityError):
            ClienteEtiqueta.objects.create(
                empresa=self.empresa,
                cliente=self.cliente,
                etiqueta=self.etiqueta,
            )

    def test_clean_rechaza_etiqueta_de_otra_empresa(self):
        """clean() must reject a tag that belongs to a different empresa."""
        empresa_b = make_empresa()
        etiqueta_b = make_etiqueta(empresa_b)

        ce = ClienteEtiqueta(
            empresa=self.empresa,
            cliente=self.cliente,
            etiqueta=etiqueta_b,
        )
        with self.assertRaises(ValidationError):
            ce.clean()


class NotaClienteModelTest(TestCase):
    """Tests for the NotaCliente model."""

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)
        self.cliente = make_cliente(self.empresa)

    def test_crear_nota(self):
        nota = NotaCliente.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            contenido="Primera nota.",
            created_by=self.usuario,
            updated_by=self.usuario,
        )
        self.assertEqual(nota.contenido, "Primera nota.")
        self.assertEqual(nota.cliente, self.cliente)

    def test_nota_soft_deleteable(self):
        nota = NotaCliente.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            contenido="Nota borrable.",
        )
        nota.soft_delete()
        self.assertTrue(nota.is_deleted)


class HistorialClienteModelTest(TestCase):
    """Tests for the HistorialCliente model."""

    def setUp(self):
        self.empresa = make_empresa()
        self.cliente = make_cliente(self.empresa)

    def test_crear_evento_historial(self):
        evento = HistorialCliente.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
            descripcion="Cliente creado.",
            metadata={"email": "x@test.com"},
        )
        self.assertEqual(evento.tipo_evento, "CREATED")
        self.assertEqual(evento.metadata["email"], "x@test.com")

    def test_historial_no_se_puede_borrar(self):
        """HistorialCliente.delete() must raise ValidationError."""
        evento = HistorialCliente.objects.create(
            empresa=self.empresa,
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
            descripcion="Evento inmutable.",
        )
        with self.assertRaises(ValidationError):
            evento.delete()

    def test_historial_se_ordena_mas_reciente_primero(self):
        """Default ordering is -created_at."""
        HistorialCliente.objects.create(
            empresa=self.empresa, cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
            descripcion="Primero",
        )
        HistorialCliente.objects.create(
            empresa=self.empresa, cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.UPDATED,
            descripcion="Segundo",
        )
        eventos = list(HistorialCliente.objects.filter(cliente=self.cliente))
        self.assertEqual(eventos[0].descripcion, "Segundo")
        self.assertEqual(eventos[1].descripcion, "Primero")
