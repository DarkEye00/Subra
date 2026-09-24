"""Time-limited links to private files: signed Spaces URLs in production, signed app URLs locally."""

from django.conf import settings
from django.core import signing
from django.core.files.storage import default_storage
from django.urls import reverse

SALT = "subra-file"


def _is_s3():
    return default_storage.__class__.__name__ == "S3Storage"


def signed_url(name, download_name=None):
    if _is_s3():
        params = {}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        return default_storage.url(name, parameters=params or None, expire=settings.SIGNED_URL_SECONDS)
    token = signing.dumps({"n": name, "d": download_name}, salt=SALT)
    return reverse("orders:file", args=[token])


def read_token(token):
    return signing.loads(token, salt=SALT, max_age=settings.SIGNED_URL_SECONDS)
