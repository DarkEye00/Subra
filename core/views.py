from django.conf import settings
from django.shortcuts import render


def _legal_context():
    return {
        "retention_days": settings.FILE_RETENTION_DAYS,
        "unpaid_hours": settings.UNPAID_RETENTION_HOURS,
        "reviewed": settings.LEGAL_PAGES_REVIEWED,
        "support_email": settings.SUPPORT_EMAIL,
        "subscriptions": settings.SUBSCRIPTIONS_ENABLED,
        "grace_days": settings.SUBSCRIPTION_GRACE_DAYS,
        "carryover": settings.CREDIT_CARRYOVER_MULTIPLE,
    }


def privacy(request):
    return render(request, "core/privacy.html", _legal_context())


def terms(request):
    return render(request, "core/terms.html", _legal_context())
