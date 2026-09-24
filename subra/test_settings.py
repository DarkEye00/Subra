import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_DEBUG", "0")

from .settings import *  # noqa: E402,F401,F403

MEDIA_ROOT = tempfile.mkdtemp(prefix="subra-test-media-")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
POLISHER_PLANNER = "heuristic"
ADMIN_ALERT_EMAIL = "ops@example.com"
INTASEND_ENV = "sandbox"
INTASEND_PUBLISHABLE_KEY = "ISPubKey_test"
INTASEND_SECRET_KEY = "ISSecretKey_test"
INTASEND_WEBHOOK_SECRET = "challenge-123"
