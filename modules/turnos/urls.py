"""
modules/turnos/urls.py

URL routing for the turnos module.

Registered in config/urls.py as:
    path("api/v1/", include("modules.turnos.urls"))

All routes are therefore prefixed with /api/v1/.

Generated routes
────────────────────────────────────────────────────────────────────
List / create (basename="turno" → url name "turno-list"):

    GET    /api/v1/turnos/                       → TurnoViewSet.list
    POST   /api/v1/turnos/                       → TurnoViewSet.create

Detail (url name "turno-detail"):

    GET    /api/v1/turnos/{id}/                  → TurnoViewSet.retrieve

State-transition actions (url names mirror method names):

    POST   /api/v1/turnos/{id}/confirmar/        → TurnoViewSet.confirmar
    POST   /api/v1/turnos/{id}/cancelar/         → TurnoViewSet.cancelar
    POST   /api/v1/turnos/{id}/reprogramar/      → TurnoViewSet.reprogramar
    POST   /api/v1/turnos/{id}/completar/        → TurnoViewSet.completar
    POST   /api/v1/turnos/{id}/ausente/          → TurnoViewSet.ausente

Availability query (detail=False → list-level route):

    GET    /api/v1/turnos/slots/                 → TurnoViewSet.slots

Not exposed (intentionally omitted):
    PUT    /api/v1/turnos/{id}/                  → not registered (use actions)
    PATCH  /api/v1/turnos/{id}/                  → not registered (use actions)
    DELETE /api/v1/turnos/{id}/                  → not registered (use cancelar)
"""

from rest_framework.routers import DefaultRouter

from modules.turnos.api.views import TurnoViewSet

router = DefaultRouter()
router.register(r"turnos", TurnoViewSet, basename="turno")

urlpatterns = router.urls
