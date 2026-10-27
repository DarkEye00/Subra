from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = "accounts"
urlpatterns = [
    path("plans/", views.plans, name="plans"),
    path("plans/subscribe/", views.subscribe, name="subscribe"),
    path("plan/", views.plan, name="plan"),
    path("plan/action/", views.plan_action, name="plan_action"),
    path("plan/top-up/", views.topup, name="topup"),
    path("plan/usage.csv", views.usage_csv, name="usage_csv"),
    path("plan/invite/", views.invite, name="invite"),
    path("plan/invites/<int:invite_id>/revoke/", views.revoke_invite, name="revoke_invite"),
    path("plan/members/<int:user_id>/remove/", views.remove_member, name="remove_member"),
    path("plan/dev/", views.dev_subscribe, name="dev_subscribe"),
    path("invites/<str:token>/", views.invite_accept, name="invite_accept"),
    # Password reset (Django's built-in flow, emailed)
    path("password/reset/", auth_views.PasswordResetView.as_view(
        template_name="accounts/password_reset.html", email_template_name="accounts/password_reset_email.txt",
        subject_template_name="accounts/password_reset_subject.txt",
        success_url=reverse_lazy("accounts:password_reset_done")), name="password_reset"),
    path("password/reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="accounts/password_reset_done.html"), name="password_reset_done"),
    path("password/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="accounts/password_reset_confirm.html",
        success_url=reverse_lazy("accounts:password_reset_complete")), name="password_reset_confirm"),
    path("password/reset/done/", auth_views.PasswordResetCompleteView.as_view(
        template_name="accounts/password_reset_complete.html"), name="password_reset_complete"),
]
