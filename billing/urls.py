from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("mpesa/callback/<str:secret>/", views.mpesa_callback, name="mpesa_callback"),
]
