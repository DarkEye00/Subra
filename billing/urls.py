from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("webhooks/<slug:provider>/", views.webhook, name="webhook"),
]
