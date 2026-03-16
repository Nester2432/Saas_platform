from django.urls import path, include

urlpatterns = [
    path("", include("modules.cobranzas.api.urls")),
]
