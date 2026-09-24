from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("webhooks/intasend/", views.intasend_webhook, name="intasend_webhook"),
]
