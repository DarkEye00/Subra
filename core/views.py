from django.conf import settings
from django.shortcuts import render


def _legal_context():
    return {
        "retention_days": settings.FILE_RETENTION_DAYS,
        "reviewed": settings.LEGAL_PAGES_REVIEWED,
        "support_email": settings.SUPPORT_EMAIL,
    }


def privacy(request):
    return render(request, "core/privacy.html", _legal_context())


def terms(request):
    return render(request, "core/terms.html", _legal_context())
