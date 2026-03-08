"""
modules/turnos/models.py

Appointment scheduling module models.

All models inherit from EmpresaModel which provides:
    - empresa FK         (tenant isolation — all queries start here)
    - UUID primary key   (no sequential ID leakage)
    - created_at / updated_at / deleted_at  (soft delete + timestamps)
    - created_by / updated_by              (audit trail)

Model map:
    Servicio             → what is offered ("Haircut", "Medical consultation")
    Profesional          → who performs it ("Dr. García", "Stylist Ana")
    ProfesionalServicio  → M2M join: which professional offers which service
                           (with optional per-professional duration override)
    HorarioDisponible    → recurring weekly schedule ("Mon 09:00–18:00")
    BloqueoHorario       → one-off absences ("Holiday Dec 25", "Lunch break")
    Turno                → the concrete appointment event

Concurrency:
    Double booking is prevented at the service layer using select_for_update().
    The DB-level CheckConstraints act as a last line of defense for invariants
    that can be expressed as single-row predicates (fin > inicio, valid states).
    Range-overlap constraints require select_for_update in the service — see
    services.py for the full explanation.

Index strategy:
    Every index starts with `empresa` — in a multi-tenant shared DB every query
    is always tenant-scoped first. Single-column indexes on tenant tables are
    almost never useful; composite (empresa, field) indexes cover all cases.
"""

from django.db import models
from django.core.exceptions import ValidationError

from core.models import EmpresaModel


# ---------------------------------------------------------------------------
# Choice enumerations
# ---------------------------------------------------------------------------

class DiaSemana(models.IntegerChoices):
    """
    ISO weekday numbering: Monday=0 … Sunday=6.

    Matches Python's datetime.weekday() so day lookups don't need conversion:
        datetime.today().weekday() == DiaSemana.LUNES  →  True on Mondays
    """
    LUNES     = 0, "Lunes"
    MARTES    = 1, "Martes"
    MIERCOLES = 2, "Miércoles"
    JUEVES    = 3, "Jueves"
    VIERNES   = 4, "Viernes"
    SABADO    = 5, "Sábado"
    DOMINGO   = 6, "Domingo"


class EstadoTurno(models.TextChoices):
    """
    Finite state machine for a Turno.

    Valid transitions (enforced in TurnoService):

        PENDIENTE ──confirm──► CONFIRMADO ──completar──► COMPLETADO
            │                       │
            └────────────────────────────────cancelar──► CANCELADO
                                                              ▲
        PENDIENTE ──cancelar──────────────────────────────────┘
        CONFIRMADO ──ausente──────────────────────────────► AUSENTE

    Terminal states (no further transitions allowed):
        COMPLETADO, CANCELADO, AUSENTE
    """
    PENDIENTE   = "PENDIENTE",   "Pendiente de confirmación"
    CONFIRMADO  = "CONFIRMADO",  "Confirmado"
    COMPLETADO  = "COMPLETADO",  "Completado"
    CANCELADO   = "CANCELADO",   "Cancelado"
    AUSENTE     = "AUSENTE",     "Cliente ausente"


class ActorCancelacion(models.TextChoices):
    """Who initiated a cancellation. Required when estado=CANCELADO."""
    PROFESIONAL = "PROFESIONAL", "Profesional"
    CLIENTE     = "CLIENTE",     "Cliente"
    SISTEMA     = "SISTEMA",     "Sistema (automático)"


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------

class Servicio(EmpresaModel):
    """
    A service offering within an empresa.

    Examples: "Haircut", "Medical consultation", "Massage 60 min".

    duracion_minutos is the canonical duration — used to calculate Turno.fecha_fin
    automatically when a booking is made. Individual professionals may override
    this duration via ProfesionalServicio.duracion_override.

    precio is the catalog price at definition time. The actual charged amount
    is stored in Turno.precio_final (snapshot at booking time) so historical
    records are not affected by price changes.

    color is used by front-end calendar views to visually distinguish services.
    """

    nombre = models.CharField(
        max_length=100,
        help_text="Display name shown to clients and staff."
    )
    descripcion = models.TextField(
        blank=True,
        help_text="Optional longer description of what the service includes."
    )
    duracion_minutos = models.PositiveIntegerField(
        help_text="Default duration in minutes. Used to auto-calculate Turno.fecha_fin."
    )
    precio = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Catalog price. NULL = no fixed price (negotiated per appointment)."
    )
    color = models.CharField(
        max_length=7,
        default="#3B82F6",
        help_text="Hex color for calendar display, e.g. '#3B82F6'."
    )
    activo = models.BooleanField(
        default=True,
        help_text="Inactive services cannot be booked but existing turnos are unaffected."
    )

    class Meta:
        db_table = "turnos_servicio"
        verbose_name = "Servicio"
        verbose_name_plural = "Servicios"
        ordering = ["nombre"]
        constraints = [
            # Two services in the same empresa cannot share a name (among active records).
            # Deleted services free their name for reuse.
            models.UniqueConstraint(
                fields=["empresa", "nombre"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_servicio_nombre_por_empresa",
            ),
            # Sanity: duration must be positive.
            # (PositiveIntegerField already prevents 0 at the Python level;
            #  this CheckConstraint enforces it at the DB level too.)
            models.CheckConstraint(
                check=models.Q(duracion_minutos__gt=0),
                name="check_servicio_duracion_positiva",
            ),
        ]
        indexes = [
            # List active services per empresa → most common query
            models.Index(
                fields=["empresa", "activo"],
                name="idx_servicio_empresa_activo",
            ),
            # Search by name per empresa
            models.Index(
                fields=["empresa", "nombre"],
                name="idx_servicio_empresa_nombre",
            ),
        ]

    @property
    def duracion_display(self):
        """Human-readable duration: '45 min' or '1h 30min'."""
        h, m = divmod(self.duracion_minutos, 60)
        if h and m:
            return f"{h}h {m}min"
        if h:
            return f"{h}h"
        return f"{m} min"

    def __str__(self):
        return f"{self.nombre} ({self.duracion_display})"


# ---------------------------------------------------------------------------
# Profesional
# ---------------------------------------------------------------------------

class Profesional(EmpresaModel):
    """
    A staff member who performs services and holds appointments.

    usuario FK is optional — a professional may or may not have a platform
    login. When set, it allows the professional to log in and view their own
    agenda. The constraint ensures one professional record per user per empresa.

    color_agenda is used by calendar views to distinguish professionals
    when displaying a multi-staff day view.

    servicios is the set of services this professional can perform,
    managed through ProfesionalServicio (explicit through model).
    """

    nombre = models.CharField(max_length=100)
    apellido = models.CharField(max_length=100, blank=True)
    email = models.EmailField(
        blank=True,
        help_text="Contact email. Not used for login unless usuario is set."
    )
    telefono = models.CharField(max_length=30, blank=True)
    usuario = models.ForeignKey(
        "usuarios.Usuario",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="profesionales",
        help_text="Platform account for this professional (optional)."
    )
    servicios = models.ManyToManyField(
        Servicio,
        through="ProfesionalServicio",
        related_name="profesionales",
        blank=True,
    )
    activo = models.BooleanField(
        default=True,
        help_text="Inactive professionals do not appear in booking flows."
    )
    color_agenda = models.CharField(
        max_length=7,
        default="#10B981",
        help_text="Hex color for multi-staff calendar view, e.g. '#10B981'."
    )
    notas_internas = models.TextField(
        blank=True,
        help_text="Internal notes about this professional. Not visible to clients."
    )

    class Meta:
        db_table = "turnos_profesional"
        verbose_name = "Profesional"
        verbose_name_plural = "Profesionales"
        ordering = ["apellido", "nombre"]
        constraints = [
            # A platform user can only be linked to one professional per empresa.
            # Partial: only enforced when usuario is not NULL.
            models.UniqueConstraint(
                fields=["empresa", "usuario"],
                condition=models.Q(
                    deleted_at__isnull=True,
                    usuario__isnull=False,
                ),
                name="unique_profesional_usuario_por_empresa",
            ),
        ]
        indexes = [
            # List active professionals per empresa
            models.Index(
                fields=["empresa", "activo"],
                name="idx_profesional_empresa_activo",
            ),
            # Resolve which professional a logged-in user maps to
            models.Index(
                fields=["empresa", "usuario"],
                name="idx_profesional_empresa_usuario",
            ),
            # Default sort per empresa
            models.Index(
                fields=["empresa", "apellido", "nombre"],
                name="idx_profesional_empresa_nombre",
            ),
        ]

    @property
    def nombre_completo(self):
        return f"{self.nombre} {self.apellido}".strip()

    def __str__(self):
        return self.nombre_completo


# ---------------------------------------------------------------------------
# ProfesionalServicio  (M2M through model)
# ---------------------------------------------------------------------------

class ProfesionalServicio(EmpresaModel):
    """
    Explicit M2M join between Profesional and Servicio.

    Using an explicit through model (not an implicit M2M) gives:
    - empresa FK for tenant isolation on the join itself
    - duracion_override: some professionals work faster or slower than the
      canonical Servicio.duracion_minutos. NULL means "use the service default".
    - Full audit trail (created_at, created_by) on when the assignment was made

    When computing Turno.fecha_fin, the service layer resolves duration as:
        duracion = profesional_servicio.duracion_override or servicio.duracion_minutos
    """

    profesional = models.ForeignKey(
        Profesional,
        on_delete=models.CASCADE,
        related_name="profesional_servicios",
    )
    servicio = models.ForeignKey(
        Servicio,
        on_delete=models.CASCADE,
        related_name="profesional_servicios",
    )
    duracion_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Override duration in minutes for this professional. "
            "NULL means use Servicio.duracion_minutos."
        ),
    )

    class Meta:
        db_table = "turnos_profesional_servicio"
        verbose_name = "Servicio de Profesional"
        verbose_name_plural = "Servicios de Profesionales"
        constraints = [
            # A professional can only be assigned to a service once (per empresa).
            models.UniqueConstraint(
                fields=["empresa", "profesional", "servicio"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_profesional_servicio_por_empresa",
            ),
            # If overriding duration, it must be positive.
            models.CheckConstraint(
                check=(
                    models.Q(duracion_override__isnull=True)
                    | models.Q(duracion_override__gt=0)
                ),
                name="check_profesional_servicio_duracion_override_positiva",
            ),
        ]
        indexes = [
            # "Which services does this professional offer?" (booking flow)
            models.Index(
                fields=["empresa", "profesional"],
                name="idx_profserv_empresa_profesional",
            ),
            # "Which professionals offer this service?" (availability search)
            models.Index(
                fields=["empresa", "servicio"],
                name="idx_profserv_empresa_servicio",
            ),
        ]

    def clean(self):
        """Validate that profesional and servicio belong to the same empresa."""
        if self.profesional_id and self.servicio_id:
            if self.profesional.empresa_id != self.servicio.empresa_id:
                raise ValidationError(
                    "El profesional y el servicio deben pertenecer a la misma empresa."
                )

    def __str__(self):
        override = f" ({self.duracion_override} min)" if self.duracion_override else ""
        return f"{self.profesional} → {self.servicio}{override}"


# ---------------------------------------------------------------------------
# HorarioDisponible
# ---------------------------------------------------------------------------

class HorarioDisponible(EmpresaModel):
    """
    Recurring weekly availability template for a Profesional.

    Represents a block of working hours on a given weekday.
    A professional can have multiple HorarioDisponible records per day
    (e.g. "Mon 09:00–13:00" and "Mon 15:00–19:00" for a split shift).

    Overlap between records for the same professional+day is validated
    in TurnoService, not in the DB (PostgreSQL range-overlap constraints
    require the btree_gist extension which we avoid for portability).

    This is a TEMPLATE, not a calendar. "Mon 09:00-18:00" means every Monday.
    One-off exceptions (holidays, sick days) are modelled as BloqueoHorario.
    """

    profesional = models.ForeignKey(
        Profesional,
        on_delete=models.CASCADE,
        related_name="horarios",
    )
    dia_semana = models.IntegerField(
        choices=DiaSemana.choices,
        help_text="Day of the week (0=Monday … 6=Sunday)."
    )
    hora_inicio = models.TimeField(
        help_text="Start of working block, e.g. 09:00."
    )
    hora_fin = models.TimeField(
        help_text="End of working block, e.g. 18:00."
    )
    activo = models.BooleanField(
        default=True,
        help_text=(
            "Inactive schedules are ignored in availability checks. "
            "Useful for temporary suspensions without deletion."
        ),
    )

    class Meta:
        db_table = "turnos_horario_disponible"
        verbose_name = "Horario Disponible"
        verbose_name_plural = "Horarios Disponibles"
        ordering = ["dia_semana", "hora_inicio"]
        constraints = [
            # End time must be strictly after start time.
            # This is enforceable at the DB level (single-row predicate).
            models.CheckConstraint(
                check=models.Q(hora_fin__gt=models.F("hora_inicio")),
                name="check_horario_fin_despues_de_inicio",
            ),
        ]
        indexes = [
            # "What are this professional's hours on a given day?" (booking flow)
            # The most frequent availability query: filters by empresa + profesional + day.
            models.Index(
                fields=["empresa", "profesional", "dia_semana"],
                name="idx_horario_empresa_profesional_dia",
            ),
            # "Which professionals work on this day?" (staff overview)
            models.Index(
                fields=["empresa", "dia_semana", "activo"],
                name="idx_horario_empresa_dia_activo",
            ),
        ]

    def __str__(self):
        dia = DiaSemana(self.dia_semana).label
        return f"{self.profesional} — {dia} {self.hora_inicio:%H:%M}–{self.hora_fin:%H:%M}"


# ---------------------------------------------------------------------------
# BloqueoHorario
# ---------------------------------------------------------------------------

class BloqueoHorario(EmpresaModel):
    """
    A one-off time block marking a professional as unavailable.

    Conceptually separate from HorarioDisponible (the recurring template):
    - HorarioDisponible: "I work every Monday 09:00–18:00"
    - BloqueoHorario:    "I am out Dec 25 all day" / "Lunch 13:00–14:00 on Jan 10"

    The service layer checks for active bloqueos before allowing a new Turno
    in that time range. Bloqueos take precedence over HorarioDisponible.

    todo_el_dia=True is a convenience flag: the service ignores hora fields
    and treats the professional as unavailable for the entire calendar day(s)
    spanned by [fecha_inicio.date(), fecha_fin.date()].
    """

    profesional = models.ForeignKey(
        Profesional,
        on_delete=models.CASCADE,
        related_name="bloqueos",
    )
    fecha_inicio = models.DateTimeField(
        help_text="Start of the blocked period (inclusive)."
    )
    fecha_fin = models.DateTimeField(
        help_text="End of the blocked period (exclusive)."
    )
    todo_el_dia = models.BooleanField(
        default=False,
        help_text=(
            "If True, the professional is blocked for all hours of every calendar "
            "day between fecha_inicio.date() and fecha_fin.date()."
        ),
    )
    motivo = models.CharField(
        max_length=200,
        blank=True,
        help_text="Optional reason: 'Holiday', 'Sick leave', 'Lunch break', etc."
    )

    class Meta:
        db_table = "turnos_bloqueo_horario"
        verbose_name = "Bloqueo de Horario"
        verbose_name_plural = "Bloqueos de Horario"
        ordering = ["fecha_inicio"]
        constraints = [
            # End must be strictly after start — enforced at DB level.
            models.CheckConstraint(
                check=models.Q(fecha_fin__gt=models.F("fecha_inicio")),
                name="check_bloqueo_fin_despues_de_inicio",
            ),
        ]
        indexes = [
            # "Does this professional have a bloqueo overlapping [A, B)?"
            # Overlap query: bloqueo.inicio < B AND bloqueo.fin > A
            # Both fecha_inicio and fecha_fin appear in WHERE — index both.
            models.Index(
                fields=["empresa", "profesional", "fecha_inicio"],
                name="idx_bloqueo_empresa_profesional_inicio",
            ),
            models.Index(
                fields=["empresa", "profesional", "fecha_fin"],
                name="idx_bloqueo_empresa_profesional_fin",
            ),
            # "All bloqueos in a date range for an empresa" (agenda overview)
            models.Index(
                fields=["empresa", "fecha_inicio", "fecha_fin"],
                name="idx_bloqueo_empresa_rango",
            ),
        ]

    def __str__(self):
        if self.todo_el_dia:
            return (
                f"Bloqueo {self.profesional} — "
                f"{self.fecha_inicio:%Y-%m-%d} todo el día"
            )
        return (
            f"Bloqueo {self.profesional} — "
            f"{self.fecha_inicio:%Y-%m-%d %H:%M}–{self.fecha_fin:%H:%M}"
        )


# ---------------------------------------------------------------------------
# Turno
# ---------------------------------------------------------------------------

class Turno(EmpresaModel):
    """
    A concrete appointment: a specific professional, performing a specific
    service, for a specific client, at a specific time.

    This is the central model of the module. Key design decisions:

    fecha_fin is CALCULATED by the service (fecha_inicio + effective_duration),
    never entered by the user. This guarantees that fecha_fin - fecha_inicio
    always equals the service duration, preventing phantom gaps in the schedule.

    precio_final is a SNAPSHOT of the price at booking time — not a live FK
    to Servicio.precio. If the service price changes next week, past appointments
    remain historically accurate (same rationale as in the Ventas module).

    cliente is OPTIONAL — allows:
      1. Walk-in appointments where no client record exists yet.
      2. Internal blocks that look like appointments (e.g. "Staff meeting").

    cancelado_por is required when estado=CANCELADO. Enforced by a
    CheckConstraint + service-layer validation so both DB and code agree.

    Concurrency / double booking prevention:
        The service layer uses select_for_update() to acquire a row-level
        lock on all active turnos for the same profesional in the target range
        before inserting. This serialises concurrent booking requests for the
        same professional at the DB transaction level. See services.py.
    """

    profesional = models.ForeignKey(
        Profesional,
        on_delete=models.PROTECT,
        related_name="turnos",
        help_text="Professional who will perform the service."
    )
    servicio = models.ForeignKey(
        Servicio,
        on_delete=models.PROTECT,
        related_name="turnos",
        help_text="Service being performed."
    )
    cliente = models.ForeignKey(
        "clientes.Cliente",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="turnos",
        help_text="Client receiving the service. NULL for walk-ins or internal blocks."
    )
    fecha_inicio = models.DateTimeField(
        help_text="Appointment start time (user-provided)."
    )
    fecha_fin = models.DateTimeField(
        help_text=(
            "Appointment end time (calculated by service: "
            "fecha_inicio + effective_duration_minutes). Never set manually."
        )
    )
    estado = models.CharField(
        max_length=20,
        choices=EstadoTurno.choices,
        default=EstadoTurno.PENDIENTE,
        help_text="Current state in the appointment lifecycle."
    )
    notas_internas = models.TextField(
        blank=True,
        help_text="Internal notes visible only to staff."
    )
    notas_cliente = models.TextField(
        blank=True,
        help_text="Notes or special requests from the client."
    )
    precio_final = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=(
            "Price snapshot at booking time. NULL until confirmed. "
            "Defaults to Servicio.precio but may be adjusted per appointment."
        ),
    )
    cancelado_por = models.CharField(
        max_length=20,
        choices=ActorCancelacion.choices,
        null=True,
        blank=True,
        help_text="Who cancelled the appointment. Required when estado=CANCELADO."
    )
    motivo_cancelacion = models.TextField(
        blank=True,
        help_text="Free-text reason for cancellation."
    )

    class Meta:
        db_table = "turnos_turno"
        verbose_name = "Turno"
        verbose_name_plural = "Turnos"
        ordering = ["fecha_inicio"]
        constraints = [
            # fecha_fin must be strictly after fecha_inicio.
            # The service always sets both; this is a DB-level safety net.
            models.CheckConstraint(
                check=models.Q(fecha_fin__gt=models.F("fecha_inicio")),
                name="check_turno_fin_despues_de_inicio",
            ),
            # When estado=CANCELADO, cancelado_por must be set.
            # Expressed as: NOT(estado=CANCELADO) OR cancelado_por IS NOT NULL
            # i.e. it's only a problem when both are true simultaneously.
            models.CheckConstraint(
                check=(
                    ~models.Q(estado=EstadoTurno.CANCELADO)
                    | models.Q(cancelado_por__isnull=False)
                ),
                name="check_turno_cancelado_requiere_actor",
            ),
        ]
        indexes = [
            # ── Agenda queries ──────────────────────────────────────────
            # "All appointments for this professional on this day"
            # Primary index for the agenda view and double-booking check.
            models.Index(
                fields=["empresa", "profesional", "fecha_inicio"],
                name="idx_turno_empresa_profesional_inicio",
            ),
            # "Active appointments for this professional" (estado filter)
            # Used by the anti-double-booking select_for_update query:
            #   WHERE empresa=? AND profesional=? AND estado IN (PENDIENTE, CONFIRMADO)
            #   AND fecha_inicio < new_fin AND fecha_fin > new_inicio
            models.Index(
                fields=["empresa", "profesional", "estado", "fecha_inicio"],
                name="idx_turno_empresa_profesional_estado",
            ),
            # fecha_fin needed as the right bound of the overlap check:
            #   existing.fecha_fin > new_fecha_inicio
            models.Index(
                fields=["empresa", "profesional", "fecha_fin"],
                name="idx_turno_empresa_profesional_fin",
            ),
            # ── Client history ──────────────────────────────────────────
            # "All appointments for this client" (client detail view)
            models.Index(
                fields=["empresa", "cliente", "fecha_inicio"],
                name="idx_turno_empresa_cliente_inicio",
            ),
            # ── Date-range queries ──────────────────────────────────────
            # "All appointments on a given date" (daily/weekly agenda)
            # empresa + fecha_inicio covers all date-range WHERE clauses.
            # (empresa, created_at already covered by EmpresaModel.Meta.indexes)
            models.Index(
                fields=["empresa", "fecha_inicio"],
                name="idx_turno_empresa_inicio",
            ),
            # ── Status dashboard ────────────────────────────────────────
            # "All pending/confirmed appointments per empresa" (ops dashboard)
            models.Index(
                fields=["empresa", "estado"],
                name="idx_turno_empresa_estado",
            ),
        ]

    @property
    def duracion_minutos(self):
        """Actual duration in minutes derived from the stored datetimes."""
        delta = self.fecha_fin - self.fecha_inicio
        return int(delta.total_seconds() // 60)

    @property
    def es_activo(self):
        """True if the turno is in a non-terminal, bookable state."""
        return self.estado in (EstadoTurno.PENDIENTE, EstadoTurno.CONFIRMADO)

    @property
    def es_terminal(self):
        """True if no further state transitions are allowed."""
        return self.estado in (
            EstadoTurno.COMPLETADO,
            EstadoTurno.CANCELADO,
            EstadoTurno.AUSENTE,
        )

    def __str__(self):
        cliente_str = str(self.cliente) if self.cliente_id else "Sin cliente"
        return (
            f"{self.servicio} — {self.profesional} — "
            f"{self.fecha_inicio:%Y-%m-%d %H:%M} ({cliente_str})"
        )
