"""Customer emails for subscriptions (spec, section 8). Paystack sends the charge receipts; these are ours.

Sent after the transaction commits, so SMTP latency never holds a lock or slows a webhook. Never raises.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string

log = logging.getLogger(__name__)

SUBJECTS = {
    "subscription_active": "Your SUBRA {plan} plan is active",
    "renewal_failed": "We couldn't charge your card for SUBRA",
    "grace_last_day": "Last reminder: your SUBRA credits expire tomorrow",
    "credits_expired": "Your SUBRA credits have expired",
    "cancelled": "Your SUBRA plan is cancelled",
    "renewal_reminder": "Your SUBRA plan renews on {date}",
    "invite": "{inviter} invited you to {company} on SUBRA",
}


def send(kind, to, **context):
    context.setdefault("site_url", settings.SITE_URL)
    context.setdefault("support_email", settings.SUPPORT_EMAIL)
    subject = SUBJECTS[kind].format(**{k: v for k, v in context.items() if isinstance(v, (str, int))})
    body = render_to_string(f"billing/email/{kind}.txt", context)

    def deliver():
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to])
        except Exception:
            log.exception("Could not send %s email to %s", kind, to)

    transaction.on_commit(deliver)
