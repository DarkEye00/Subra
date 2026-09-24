import logging

from django.conf import settings
from django.core.mail import send_mail

log = logging.getLogger(__name__)


def alert_admins(subject, body):
    """Email the ops address (ADMIN_ALERT_EMAIL). Never raises."""
    log.warning("ADMIN ALERT: %s\n%s", subject, body)
    if not settings.ADMIN_ALERT_EMAIL:
        return
    try:
        send_mail(f"[SUBRA] {subject}", body, settings.DEFAULT_FROM_EMAIL, [settings.ADMIN_ALERT_EMAIL])
    except Exception:
        log.exception("Could not send admin alert email")
