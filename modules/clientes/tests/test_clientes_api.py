"""
modules/clientes/tests/test_clientes_api.py

API integration tests for the clientes module.

Tests make real HTTP requests through the full Django/DRF stack:
    URL routing → permissions → ViewSet → Service → DB

This means these tests exercise:
    - TenantMiddleware (empresa resolved from JWT)
    - ModuloActivoPermission (module must be active)
    - IsTenantAuthenticated (user must belong to empresa)
    - ClienteObjectPermission (object must belong to empresa)
    - Pagination (count/next/previous/results envelope)
    - SearchFilter (?search=)
    - OrderingFilter (?ordering=)

Setup pattern:
    Each test class has a setUp that creates a fresh empresa, user,
    activates the "clientes" module, and authenticates the client.
    Cross-tenant tests create a second empresa + user to verify isolation.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.usuarios.auth.serializers import get_tokens_for_user
from modules.clientes.models import (
    Cliente,
    EtiquetaCliente,
    NotaCliente,
    HistorialCliente,
)
from modules.clientes.services import ClienteService
from modules.clientes.tests.factories import (
    make_empresa,
    make_usuario,
    make_admin,
    make_cliente,
    make_etiqueta,
    activar_modulo,
)


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class ClienteAPITestCase(APITestCase):
    """
    Base class with shared setUp for all clientes API tests.

    Provides:
        self.empresa     → active Empresa
        self.usuario     → authenticated Usuario
        self.client      → APIClient with Bearer token set

    Subclasses can override setUp and call super() to extend it.
    """

    def setUp(self):
        self.empresa = make_empresa()
        self.usuario = make_usuario(self.empresa)
        activar_modulo(self.empresa, "clientes")
        self._authenticate(self.usuario)

    def _authenticate(self, usuario):
        tokens = get_tokens_for_user(usuario)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {tokens['access']}"
        )

    def _url(self, name, **kwargs):
        return reverse(name, kwargs=kwargs)


# ---------------------------------------------------------------------------
# List & pagination
# ---------------------------------------------------------------------------

class ClienteListTest(ClienteAPITestCase):

    def test_listar_clientes_retorna_200(self):
        make_cliente(self.empresa)
        response = self.client.get(reverse("cliente-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_respuesta_tiene_envelope_paginado(self):
        """Response must have count/next/previous/results keys."""
        response = self.client.get(reverse("cliente-list"))
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)

    def test_paginacion_page_size_default_25(self):
        """Without ?page_size the default is 25 records per page."""
        for i in range(30):
            make_cliente(self.empresa, nombre=f"Cliente {i}")
        response = self.client.get(reverse("cliente-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 30)
        self.assertEqual(len(response.data["results"]), 25)

    def test_paginacion_page_size_override(self):
        """?page_size=5 returns 5 records."""
        for i in range(10):
            make_cliente(self.empresa, nombre=f"C{i}")
        response = self.client.get(reverse("cliente-list") + "?page_size=5")
        self.assertEqual(len(response.data["results"]), 5)
        self.assertIsNotNone(response.data["next"])

    def test_paginacion_page_size_max_100(self):
        """?page_size=999 must be capped at max_page_size=100."""
        for i in range(10):
            make_cliente(self.empresa)
        response = self.client.get(reverse("cliente-list") + "?page_size=999")
        # DRF caps silently — no error, just returns max_page_size
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(len(response.data["results"]), 100)

    def test_lista_solo_muestra_clientes_de_la_empresa(self):
        """Clients from other empresas must never appear in the list."""
        empresa_b = make_empresa()
        make_cliente(empresa_b)                 # another tenant
        cliente_propio = make_cliente(self.empresa)

        response = self.client.get(reverse("cliente-list"))
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(cliente_propio.id), ids)
        self.assertEqual(response.data["count"], 1)

    def test_lista_excluye_soft_deleted(self):
        """Soft-deleted clients must not appear in the default list."""
        activo = make_cliente(self.empresa)
        eliminado = make_cliente(self.empresa)
        eliminado.soft_delete()

        response = self.client.get(reverse("cliente-list"))
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(activo.id), ids)
        self.assertNotIn(str(eliminado.id), ids)

    def test_sin_autenticacion_retorna_401(self):
        self.client.credentials()  # clear token
        response = self.client.get(reverse("cliente-list"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_modulo_inactivo_retorna_403(self):
        """Request must be rejected if the clientes module is not active."""
        from apps.modulos.models import EmpresaModulo, Modulo
        EmpresaModulo.objects.filter(
            empresa=self.empresa,
            modulo__codigo="clientes",
        ).update(activo=False)
        # Clear permission cache
        from django.core.cache import cache
        cache.clear()

        response = self.client.get(reverse("cliente-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class ClienteSearchTest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.juan = make_cliente(self.empresa, nombre="Juan", apellido="Pérez", email="juan@test.com")
        self.maria = make_cliente(self.empresa, nombre="María", apellido="García", email="maria@test.com")
        self.otro = make_cliente(self.empresa, nombre="Otro", apellido="Apellido", email="otro@test.com")

    def test_busqueda_por_nombre(self):
        response = self.client.get(reverse("cliente-list") + "?search=Juan")
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(self.juan.id), ids)
        self.assertNotIn(str(self.maria.id), ids)

    def test_busqueda_por_apellido(self):
        response = self.client.get(reverse("cliente-list") + "?search=García")
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(self.maria.id), ids)
        self.assertNotIn(str(self.juan.id), ids)

    def test_busqueda_por_email(self):
        response = self.client.get(reverse("cliente-list") + "?search=juan@test.com")
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(self.juan.id), ids)

    def test_busqueda_sin_resultados(self):
        response = self.client.get(reverse("cliente-list") + "?search=zzznomatch")
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_busqueda_es_case_insensitive(self):
        """SearchFilter on PostgreSQL uses ILIKE — case-insensitive."""
        response = self.client.get(reverse("cliente-list") + "?search=JUAN")
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(self.juan.id), ids)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

class ClienteOrderingTest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.c_z = make_cliente(self.empresa, nombre="Z", apellido="Zeta")
        self.c_a = make_cliente(self.empresa, nombre="A", apellido="Alfa")

    def test_ordering_default_apellido(self):
        response = self.client.get(reverse("cliente-list"))
        apellidos = [c["apellido"] for c in response.data["results"]]
        self.assertEqual(apellidos, sorted(apellidos))

    def test_ordering_por_nombre(self):
        response = self.client.get(reverse("cliente-list") + "?ordering=nombre")
        nombres = [c["nombre"] for c in response.data["results"]]
        self.assertEqual(nombres, sorted(nombres))

    def test_ordering_por_created_at_desc(self):
        response = self.client.get(reverse("cliente-list") + "?ordering=-created_at")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # First result should be the most recently created (c_a, created after c_z)
        self.assertEqual(response.data["results"][0]["id"], str(self.c_a.id))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class ClienteCreateTest(ClienteAPITestCase):

    def test_crear_cliente_retorna_201(self):
        payload = {"nombre": "Nuevo", "apellido": "Cliente", "email": "nuevo@test.com"}
        response = self.client.post(reverse("cliente-list"), payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", response.data)

    def test_crear_cliente_persiste_en_db(self):
        self.client.post(reverse("cliente-list"), {"nombre": "Guardado"})
        self.assertTrue(Cliente.objects.for_empresa(self.empresa).filter(nombre="Guardado").exists())

    def test_crear_cliente_asigna_empresa_automaticamente(self):
        """empresa must be set from request.empresa, never from the request body."""
        response = self.client.post(reverse("cliente-list"), {"nombre": "Auto"})
        cliente_id = response.data["id"]
        cliente = Cliente.objects.get(id=cliente_id)
        self.assertEqual(cliente.empresa, self.empresa)

    def test_crear_cliente_email_duplicado_retorna_400(self):
        make_cliente(self.empresa, email="taken@test.com")
        response = self.client.post(
            reverse("cliente-list"),
            {"nombre": "Dup", "email": "taken@test.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_crear_cliente_sin_nombre_retorna_400(self):
        response = self.client.post(reverse("cliente-list"), {"email": "no-nombre@test.com"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_crear_cliente_registra_evento_historial(self):
        self.client.post(reverse("cliente-list"), {"nombre": "Con Historial"})
        cliente = Cliente.objects.for_empresa(self.empresa).get(nombre="Con Historial")
        evento = HistorialCliente.objects.filter(
            cliente=cliente,
            tipo_evento=HistorialCliente.TipoEvento.CREATED,
        ).first()
        self.assertIsNotNone(evento)


# ---------------------------------------------------------------------------
# Retrieve & update
# ---------------------------------------------------------------------------

class ClienteRetrieveUpdateTest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.cliente = make_cliente(self.empresa, nombre="Original", email="orig@test.com")

    def test_retrieve_retorna_200(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(self.cliente.id))

    def test_retrieve_cliente_otra_empresa_retorna_404(self):
        """A user must not be able to retrieve a client from another empresa."""
        empresa_b = make_empresa()
        cliente_b = make_cliente(empresa_b)
        url = reverse("cliente-detail", kwargs={"pk": cliente_b.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_partial_update_retorna_200(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        response = self.client.patch(url, {"nombre": "Actualizado"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["nombre"], "Actualizado")

    def test_partial_update_persiste_en_db(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        self.client.patch(url, {"apellido": "NuevoApellido"})
        self.cliente.refresh_from_db()
        self.assertEqual(self.cliente.apellido, "NuevoApellido")

    def test_partial_update_registra_evento_historial(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        self.client.patch(url, {"nombre": "Otro"})
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.UPDATED,
        ).first()
        self.assertIsNotNone(evento)


# ---------------------------------------------------------------------------
# Soft delete
# ---------------------------------------------------------------------------

class ClienteDestroyTest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.cliente = make_cliente(self.empresa)

    def test_destroy_retorna_204(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_destroy_es_soft_delete_no_hard_delete(self):
        """DELETE must soft-delete — record must still exist in DB with deleted_at set."""
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        self.client.delete(url)
        # Use with_deleted() to bypass the manager filter
        cliente_db = Cliente.objects.with_deleted().get(id=self.cliente.id)
        self.assertIsNotNone(cliente_db.deleted_at)

    def test_destroy_cliente_no_aparece_en_lista_posterior(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente.id})
        self.client.delete(url)
        list_response = self.client.get(reverse("cliente-list"))
        ids = [c["id"] for c in list_response.data["results"]]
        self.assertNotIn(str(self.cliente.id), ids)

    def test_destroy_cliente_otra_empresa_retorna_404(self):
        empresa_b = make_empresa()
        cliente_b = make_cliente(empresa_b)
        url = reverse("cliente-detail", kwargs={"pk": cliente_b.id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Notas endpoint
# ---------------------------------------------------------------------------

class NotasClienteAPITest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.cliente = make_cliente(self.empresa)
        self.url_notas = reverse("cliente-notas", kwargs={"pk": self.cliente.id})

    def test_listar_notas_retorna_200(self):
        response = self.client.get(self.url_notas)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_listar_notas_paginadas(self):
        """Notes list must return the paginated envelope."""
        self.assertIn("results", self.client.get(self.url_notas).data)

    def test_agregar_nota_retorna_201(self):
        response = self.client.post(self.url_notas, {"contenido": "Nueva nota."})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", response.data)
        self.assertEqual(response.data["contenido"], "Nueva nota.")

    def test_agregar_nota_persiste_en_db(self):
        self.client.post(self.url_notas, {"contenido": "Nota guardada."})
        self.assertTrue(
            NotaCliente.objects.filter(
                cliente=self.cliente, contenido="Nota guardada."
            ).exists()
        )

    def test_agregar_nota_registra_evento_historial(self):
        self.client.post(self.url_notas, {"contenido": "Nota."})
        evento = HistorialCliente.objects.filter(
            cliente=self.cliente,
            tipo_evento=HistorialCliente.TipoEvento.NOTE_ADDED,
        ).first()
        self.assertIsNotNone(evento)

    def test_agregar_nota_contenido_vacio_retorna_400(self):
        response = self.client.post(self.url_notas, {"contenido": "  "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_agregar_nota_sin_campo_retorna_400(self):
        response = self.client.post(self.url_notas, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_listar_notas_solo_del_cliente_correcto(self):
        """Notes from other clients must not appear."""
        otro_cliente = make_cliente(self.empresa)
        ClienteService.agregar_nota(self.cliente, "Nota A", self.usuario)
        ClienteService.agregar_nota(otro_cliente, "Nota B", self.usuario)

        response = self.client.get(self.url_notas)
        contenidos = [n["contenido"] for n in response.data["results"]]
        self.assertIn("Nota A", contenidos)
        self.assertNotIn("Nota B", contenidos)

    def test_notas_cliente_otra_empresa_retorna_404(self):
        empresa_b = make_empresa()
        cliente_b = make_cliente(empresa_b)
        url = reverse("cliente-notas", kwargs={"pk": cliente_b.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Etiquetas endpoint
# ---------------------------------------------------------------------------

class EtiquetasClienteAPITest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.cliente = make_cliente(self.empresa)
        self.etiqueta = make_etiqueta(self.empresa, nombre="VIP")
        self.url_etiquetas = reverse("cliente-etiquetas", kwargs={"pk": self.cliente.id})

    def test_listar_etiquetas_retorna_200(self):
        response = self.client.get(self.url_etiquetas)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_agregar_etiqueta_retorna_201(self):
        response = self.client.post(
            self.url_etiquetas,
            {"etiqueta_id": str(self.etiqueta.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_agregar_etiqueta_aparece_en_lista(self):
        self.client.post(self.url_etiquetas, {"etiqueta_id": str(self.etiqueta.id)})
        response = self.client.get(self.url_etiquetas)
        nombres = [e["nombre"] for e in response.data]
        self.assertIn("VIP", nombres)

    def test_agregar_etiqueta_otra_empresa_retorna_404(self):
        empresa_b = make_empresa()
        etiqueta_b = make_etiqueta(empresa_b)
        response = self.client.post(
            self.url_etiquetas,
            {"etiqueta_id": str(etiqueta_b.id)}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_quitar_etiqueta_retorna_204(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        url = reverse(
            "cliente-quitar-etiqueta",
            kwargs={"pk": self.cliente.id, "etiqueta_id": str(self.etiqueta.id)}
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_quitar_etiqueta_ya_no_aparece_en_lista(self):
        ClienteService.agregar_etiqueta(self.cliente, self.etiqueta)
        url = reverse(
            "cliente-quitar-etiqueta",
            kwargs={"pk": self.cliente.id, "etiqueta_id": str(self.etiqueta.id)}
        )
        self.client.delete(url)
        response = self.client.get(self.url_etiquetas)
        self.assertEqual(response.data, [])


# ---------------------------------------------------------------------------
# Historial endpoint
# ---------------------------------------------------------------------------

class HistorialClienteAPITest(ClienteAPITestCase):

    def setUp(self):
        super().setUp()
        self.cliente = make_cliente(self.empresa)
        self.url_historial = reverse("cliente-historial", kwargs={"pk": self.cliente.id})

    def test_historial_retorna_200(self):
        response = self.client.get(self.url_historial)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_historial_paginado(self):
        response = self.client.get(self.url_historial)
        self.assertIn("results", response.data)
        self.assertIn("count", response.data)

    def test_historial_contiene_evento_created(self):
        """Creating a client via the API must register a CREATED event visible in historial."""
        nuevo = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Con Historial"},
            usuario=self.usuario,
        )
        url = reverse("cliente-historial", kwargs={"pk": nuevo.id})
        response = self.client.get(url)
        tipos = [e["tipo_evento"] for e in response.data["results"]]
        self.assertIn("CREATED", tipos)

    def test_historial_contiene_eventos_multiple(self):
        """Each service mutation produces one more historial event."""
        ClienteService.agregar_nota(self.cliente, "Nota.", self.usuario)
        etiqueta = make_etiqueta(self.empresa)
        ClienteService.agregar_etiqueta(self.cliente, etiqueta, self.usuario)

        response = self.client.get(self.url_historial)
        tipos = {e["tipo_evento"] for e in response.data["results"]}
        self.assertIn("NOTE_ADDED", tipos)
        self.assertIn("TAG_ADDED", tipos)

    def test_historial_ordenado_mas_reciente_primero(self):
        """Historial must be ordered -created_at."""
        ClienteService.agregar_nota(self.cliente, "Primera.", self.usuario)
        ClienteService.actualizar_cliente(self.cliente, {"nombre": "X"}, self.usuario)

        response = self.client.get(self.url_historial)
        tipos = [e["tipo_evento"] for e in response.data["results"]]
        # UPDATED was created after NOTE_ADDED, so it should appear first
        self.assertEqual(tipos[0], "UPDATED")

    def test_historial_cliente_otra_empresa_retorna_404(self):
        empresa_b = make_empresa()
        cliente_b = make_cliente(empresa_b)
        url = reverse("cliente-historial", kwargs={"pk": cliente_b.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Tenant isolation — cross-cutting concern
# ---------------------------------------------------------------------------

class TenantIsolationTest(ClienteAPITestCase):
    """
    Explicit cross-tenant isolation tests.
    User from empresa A must not be able to access, modify or
    see data belonging to empresa B under any endpoint.
    """

    def setUp(self):
        super().setUp()
        # Set up a second empresa with its own data
        self.empresa_b = make_empresa()
        self.usuario_b = make_usuario(self.empresa_b)
        activar_modulo(self.empresa_b, "clientes")
        self.cliente_b = make_cliente(self.empresa_b, nombre="Cliente B")

    def test_usuario_a_no_ve_clientes_de_empresa_b_en_lista(self):
        response = self.client.get(reverse("cliente-list"))
        ids = [c["id"] for c in response.data["results"]]
        self.assertNotIn(str(self.cliente_b.id), ids)

    def test_usuario_a_no_puede_retrieve_cliente_de_empresa_b(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente_b.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_usuario_a_no_puede_modificar_cliente_de_empresa_b(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente_b.id})
        response = self.client.patch(url, {"nombre": "Hackeado"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Verify nothing was actually changed
        self.cliente_b.refresh_from_db()
        self.assertEqual(self.cliente_b.nombre, "Cliente B")

    def test_usuario_a_no_puede_borrar_cliente_de_empresa_b(self):
        url = reverse("cliente-detail", kwargs={"pk": self.cliente_b.id})
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # Verify not soft-deleted
        self.cliente_b.refresh_from_db()
        self.assertIsNone(self.cliente_b.deleted_at)

    def test_usuario_b_tiene_su_propia_vista_aislada(self):
        """Authenticating as usuario_b shows only empresa_b's clients."""
        self._authenticate(self.usuario_b)
        response = self.client.get(reverse("cliente-list"))
        ids = [c["id"] for c in response.data["results"]]
        self.assertIn(str(self.cliente_b.id), ids)
        # Must not see any of empresa_a's clients
        clientes_a = Cliente.objects.for_empresa(self.empresa).values_list("id", flat=True)
        for cid in clientes_a:
            self.assertNotIn(str(cid), ids)


# ---------------------------------------------------------------------------
# N+1 query prevention
# ---------------------------------------------------------------------------

class ClienteQueryCountTest(ClienteAPITestCase):
    """
    Performance regression tests — prevent N+1 query regressions.

    Strategy
    --------
    Each test uses assertNumQueries to pin the exact query count for a given
    endpoint. If a future change breaks prefetching (e.g. removing a
    prefetch_related, adding a serializer field that hits the DB per object),
    these tests fail immediately instead of silently degrading in production.

    How the query budget is calculated
    -----------------------------------
    Each request goes through the following layers, each with a known cost:

        Layer                           Queries     Notes
        ─────────────────────────────── ────────    ─────────────────────────────
        TenantMiddleware                  1         SELECT empresa (cache miss)
        ModuloActivoPermission            1         SELECT empresa_modulo (cache miss)
        Pagination COUNT                  1         SELECT COUNT(*) clientes
        Main queryset (paginated)         1         SELECT clientes LIMIT N
        select_related("created_by")      0         Resolved in the same JOIN
        prefetch_related("etiquetas")     1         SELECT etiquetas IN (ids)
        prefetch_related("notas_detalle") 1         SELECT notas IN (ids)
        prefetch_related("historial")     1         SELECT historial IN (ids)
        ─────────────────────────────── ────────
        Total for list                    7

        For retrieve (single object):
        TenantMiddleware                  1
        ModuloActivoPermission            1
        get_object() lookup               1         SELECT cliente WHERE pk=?
        select_related("created_by")      0         JOINed in get_object query
        prefetch_related("etiquetas")     1
        prefetch_related("notas_detalle") 1
        prefetch_related("historial")     1
        ─────────────────────────────── ────────
        Total for retrieve                6

    The key proof of no N+1: query count is CONSTANT regardless of how many
    clients, tags, notes or events are in the database.

    Cache discipline
    ----------------
    cache.clear() is called in setUp so every test starts from a cold cache.
    This gives deterministic query counts — without it, middleware and
    permission queries may or may not be present depending on test order.
    """

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    # ------------------------------------------------------------------
    # List endpoint — N+1 proof
    # ------------------------------------------------------------------

    def test_list_query_count_un_cliente_sin_relaciones(self):
        """
        Baseline: 1 client, no tags/notes/events.

        Expected queries:
          1  SELECT empresa            (TenantMiddleware, cache miss)
          2  SELECT empresa_modulo     (ModuloActivoPermission, cache miss)
          3  SELECT COUNT(*)           (pagination)
          4  SELECT clientes LIMIT 25  (paginated fetch)
          5  SELECT etiquetas IN (…)   (prefetch — returns empty, still 1 query)
          6  SELECT notas IN (…)       (prefetch — returns empty, still 1 query)
          7  SELECT historial IN (…)   (prefetch — returns empty, still 1 query)

        Note: select_related("created_by") adds 0 extra queries — Django
        resolves it with a JOIN in query #4, not a separate SELECT.
        """
        make_cliente(self.empresa)
        with self.assertNumQueries(7):
            self.client.get(reverse("cliente-list"))

    def test_list_query_count_es_constante_con_multiples_clientes(self):
        """
        Core N+1 test: 10 clients with tags, notes and history each.

        If prefetch_related were missing, Django would fire 3 extra queries
        PER CLIENT (etiquetas, notas, historial) → 3 + 10×3 = 33 queries.

        With prefetch_related the count stays at 7 regardless of N.
        """
        etiqueta = make_etiqueta(self.empresa)
        for i in range(10):
            c = make_cliente(self.empresa, nombre=f"Cliente {i}")
            ClienteService.agregar_etiqueta(c, etiqueta, self.usuario)
            ClienteService.agregar_nota(c, f"Nota de {c.nombre}", self.usuario)
            # agregar_nota also fires registrar_evento → historial row added

        with self.assertNumQueries(7):
            response = self.client.get(reverse("cliente-list"))

        self.assertEqual(response.data["count"], 10)

    def test_list_query_count_permanece_constante_al_escalar(self):
        """
        Scaling proof: 25 clients (full page) must cost the same as 1.

        This test documents the O(1) vs O(N) boundary explicitly.
        A reviewer reading this test understands that any change which
        makes this fail has introduced an O(N) regression.
        """
        etiqueta_a = make_etiqueta(self.empresa, nombre="A")
        etiqueta_b = make_etiqueta(self.empresa, nombre="B")

        for i in range(25):
            c = make_cliente(self.empresa, nombre=f"C{i:02d}")
            ClienteService.agregar_etiqueta(c, etiqueta_a, self.usuario)
            ClienteService.agregar_etiqueta(c, etiqueta_b, self.usuario)
            ClienteService.agregar_nota(c, "Nota 1.", self.usuario)
            ClienteService.agregar_nota(c, "Nota 2.", self.usuario)

        with self.assertNumQueries(7):
            response = self.client.get(reverse("cliente-list"))

        # Verify a full page was actually returned (not an empty response)
        self.assertEqual(len(response.data["results"]), 25)

    def test_list_etiquetas_en_respuesta_sin_query_extra(self):
        """
        Verify that etiquetas are present in the serialized output AND
        that they came from the prefetch cache (not a fresh per-object query).

        The assertNumQueries budget proves the latter:
        if etiquetas were fetched per-object, the count would be > 7.
        """
        etiqueta = make_etiqueta(self.empresa, nombre="VIP")
        for i in range(5):
            c = make_cliente(self.empresa, nombre=f"C{i}")
            ClienteService.agregar_etiqueta(c, etiqueta, self.usuario)

        with self.assertNumQueries(7):
            response = self.client.get(reverse("cliente-list"))

        # Confirm etiquetas actually appear in the serialized output
        for item in response.data["results"]:
            self.assertEqual(len(item["etiquetas"]), 1)
            self.assertEqual(item["etiquetas"][0]["nombre"], "VIP")

    def test_list_totales_notas_historial_sin_query_extra(self):
        """
        ClienteSerializer.get_total_notas() and get_total_eventos() have two
        code paths: prefetch cache hit (O(1)) or fallback COUNT query (O(N)).

        This test proves the prefetch path is taken — if the fallback were used,
        each of 5 clients would fire 2 extra queries → count would be 7 + 10 = 17.
        """
        for i in range(5):
            c = make_cliente(self.empresa, nombre=f"C{i}")
            ClienteService.agregar_nota(c, "Nota.", self.usuario)

        with self.assertNumQueries(7):
            response = self.client.get(reverse("cliente-list"))

        # Verify the counts are correct (not just zero from a bad cache read)
        for item in response.data["results"]:
            self.assertGreaterEqual(item["total_notas"], 1)
            self.assertGreaterEqual(item["total_eventos"], 1)

    # ------------------------------------------------------------------
    # Retrieve endpoint
    # ------------------------------------------------------------------

    def test_retrieve_query_count_cliente_sin_relaciones(self):
        """
        Baseline retrieve for a client with no tags, notes or events.

        Expected queries:
          1  SELECT empresa            (TenantMiddleware, cache miss)
          2  SELECT empresa_modulo     (ModuloActivoPermission, cache miss)
          3  SELECT cliente WHERE pk=? (get_object — includes created_by JOIN)
          4  SELECT etiquetas          (prefetch)
          5  SELECT notas              (prefetch)
          6  SELECT historial          (prefetch)
        """
        cliente = make_cliente(self.empresa)
        url = reverse("cliente-detail", kwargs={"pk": cliente.id})

        with self.assertNumQueries(6):
            self.client.get(url)

    def test_retrieve_query_count_con_notas(self):
        """
        Retrieve a client that has notes attached.

        Query count must remain 6 regardless of note count.
        If NotaCliente were fetched lazily inside the serializer, each note
        would fire a SELECT for its created_by → O(N) regression.
        """
        cliente = make_cliente(self.empresa)
        for i in range(5):
            ClienteService.agregar_nota(cliente, f"Nota {i}.", self.usuario)

        url = reverse("cliente-detail", kwargs={"pk": cliente.id})

        with self.assertNumQueries(6):
            response = self.client.get(url)

        # Sanity: notes endpoint is separate; detail only returns total_notas count
        self.assertEqual(response.data["total_notas"], 5)

    def test_retrieve_query_count_con_historial(self):
        """
        Retrieve a client that has a rich event history.

        Each service call (crear, agregar_nota, actualizar) adds a historial row.
        Query count must remain 6 regardless of event count.
        """
        cliente = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Rico", "email": "rico@test.com"},
            usuario=self.usuario,
        )
        ClienteService.agregar_nota(cliente, "Nota.", self.usuario)
        ClienteService.actualizar_cliente(cliente, {"nombre": "Rico V2"}, self.usuario)
        etiqueta = make_etiqueta(self.empresa)
        ClienteService.agregar_etiqueta(cliente, etiqueta, self.usuario)

        # Reset cache — we want to count queries from a cold start
        from django.core.cache import cache
        cache.clear()

        url = reverse("cliente-detail", kwargs={"pk": cliente.id})

        with self.assertNumQueries(6):
            response = self.client.get(url)

        self.assertGreaterEqual(response.data["total_eventos"], 4)

    def test_retrieve_query_count_con_multiples_etiquetas(self):
        """
        Retrieve a client with several tags attached.

        prefetch_related("etiquetas") must resolve all tags in a single IN query,
        not one query per tag. If tags were fetched lazily via the M2M manager,
        each tag would cost 1 extra query.
        """
        cliente = make_cliente(self.empresa)
        for i in range(6):
            etiqueta = make_etiqueta(self.empresa, nombre=f"Tag {i}")
            ClienteService.agregar_etiqueta(cliente, etiqueta, self.usuario)

        url = reverse("cliente-detail", kwargs={"pk": cliente.id})

        with self.assertNumQueries(6):
            response = self.client.get(url)

        self.assertEqual(len(response.data["etiquetas"]), 6)

    # ------------------------------------------------------------------
    # Notas sub-endpoint
    # ------------------------------------------------------------------

    def test_notas_endpoint_query_count(self):
        """
        GET /clientes/{id}/notas/ — verify query count for the nested list.

        Expected queries:
          1  SELECT empresa            (TenantMiddleware, cache miss)
          2  SELECT empresa_modulo     (ModuloActivoPermission, cache miss)
          3  SELECT cliente WHERE pk=? (get_object for permission check)
          4  SELECT COUNT(*)           (SmallPagination count)
          5  SELECT notas LIMIT 10     (paginated fetch with select_related)

        NotaClienteSerializer.get_autor() reads obj.created_by — this is
        safe because the notas queryset uses select_related("created_by"),
        so no extra query fires per note.
        """
        cliente = make_cliente(self.empresa)
        for i in range(5):
            ClienteService.agregar_nota(cliente, f"Nota {i}.", self.usuario)

        url = reverse("cliente-notas", kwargs={"pk": cliente.id})

        with self.assertNumQueries(5):
            response = self.client.get(url)

        self.assertEqual(response.data["count"], 5)
        # Verify autor is resolved (would be None if select_related were missing
        # and created_by_id were accessed on an unloaded relation)
        for nota in response.data["results"]:
            self.assertIsNotNone(nota["autor"])

    def test_notas_endpoint_query_count_es_constante(self):
        """
        N+1 proof for the notas sub-endpoint: 10 notes must cost the same as 1.
        """
        cliente = make_cliente(self.empresa)
        for i in range(10):
            ClienteService.agregar_nota(cliente, f"Nota {i}.", self.usuario)

        url = reverse("cliente-notas", kwargs={"pk": cliente.id})

        with self.assertNumQueries(5):
            self.client.get(url)

    # ------------------------------------------------------------------
    # Historial sub-endpoint
    # ------------------------------------------------------------------

    def test_historial_endpoint_query_count(self):
        """
        GET /clientes/{id}/historial/ — verify query count for the event log.

        Expected queries:
          1  SELECT empresa
          2  SELECT empresa_modulo
          3  SELECT cliente WHERE pk=?
          4  SELECT COUNT(*)  (SmallPagination)
          5  SELECT historial LIMIT 10

        HistorialClienteSerializer.get_autor() reads created_by —
        the historial queryset uses select_related("created_by"),
        so it resolves in the same query as #5 (JOIN), not N extra queries.
        """
        cliente = ClienteService.crear_cliente(
            empresa=self.empresa,
            datos={"nombre": "Historial"},
            usuario=self.usuario,
        )
        ClienteService.agregar_nota(cliente, "Nota.", self.usuario)
        ClienteService.actualizar_cliente(cliente, {"apellido": "X"}, self.usuario)

        from django.core.cache import cache
        cache.clear()

        url = reverse("cliente-historial", kwargs={"pk": cliente.id})

        with self.assertNumQueries(5):
            response = self.client.get(url)

        self.assertGreaterEqual(response.data["count"], 3)

    def test_historial_endpoint_query_count_es_constante(self):
        """
        N+1 proof for historial: many events must not add queries.
        """
        cliente = make_cliente(self.empresa)
        for i in range(8):
            ClienteService.agregar_nota(cliente, f"Nota {i}.", self.usuario)

        from django.core.cache import cache
        cache.clear()

        url = reverse("cliente-historial", kwargs={"pk": cliente.id})

        with self.assertNumQueries(5):
            self.client.get(url)
