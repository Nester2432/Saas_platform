from django.urls import path, include

urlpatterns = [
    path("", include("modules.pagos.api.urls")),
]
