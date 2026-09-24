"""Daily AI spend cap (AI_DAILY_CAP_USD). Paid orders wait in the queue when it's reached."""

from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.alerts import alert_admins

from .models import DailySpend


class SpendCapReached(Exception):
    pass


def _today():
    return timezone.localdate()


def spent_today():
    row = DailySpend.objects.filter(date=_today()).first()
    return row.total_cost_usd if row else Decimal("0")


def cap_reached():
    return spent_today() >= Decimal(str(settings.AI_DAILY_CAP_USD))


def record_spend(cost_usd):
    DailySpend.objects.get_or_create(date=_today())
    DailySpend.objects.filter(date=_today()).update(total_cost_usd=F("total_cost_usd") + Decimal(str(cost_usd)))


def ensure_capacity():
    """Raise SpendCapReached (and alert once per day) if today's AI spend has hit the cap."""
    if not cap_reached():
        return
    with transaction.atomic():
        row = DailySpend.objects.select_for_update().get(date=_today())
        first_alert = not row.alert_sent
        if first_alert:
            row.alert_sent = True
            row.save(update_fields=["alert_sent"])
    if first_alert:
        alert_admins(
            "Daily AI spend cap reached",
            f"AI spend today is ${row.total_cost_usd:.2f}, at or above the cap of ${settings.AI_DAILY_CAP_USD:.2f}. "
            "New paid orders are waiting in the queue and will start when the cap resets at midnight "
            "(Nairobi time) or when you raise AI_DAILY_CAP_USD.",
        )
    raise SpendCapReached(f"Daily AI spend cap of ${settings.AI_DAILY_CAP_USD} reached")
