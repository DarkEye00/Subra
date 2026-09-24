"""Django settings. Everything environment-specific comes from env vars (see .env.example)."""

import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

from polisher.cost import PRICING

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is off")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")
SITE_URL = env("SITE_URL", "http://localhost:8000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "billing",
    "orders",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "subra.urls"
WSGI_APPLICATION = "subra.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60, conn_health_checks=True
    )
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Africa/Nairobi"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- static and media -------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_ROOT = BASE_DIR / "media"  # dev only; never served publicly (downloads go through signed views)

SPACES_BUCKET = env("SPACES_BUCKET")
if SPACES_BUCKET:
    # DigitalOcean Spaces: private bucket, signed URLs that expire after an hour
    default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "access_key": env("SPACES_KEY"),
            "secret_key": env("SPACES_SECRET"),
            "bucket_name": SPACES_BUCKET,
            "region_name": env("SPACES_REGION"),
            "endpoint_url": f"https://{env('SPACES_REGION')}.digitaloceanspaces.com",
            "default_acl": "private",
            "querystring_auth": True,
            "querystring_expire": 3600,
            "file_overwrite": False,
            "signature_version": "s3v4",
        },
    }
else:
    default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
STORAGES = {
    "default": default_storage,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}
SIGNED_URL_SECONDS = 3600

# --- cache, Celery, email ---------------------------------------------------

REDIS_URL = env("REDIS_URL")
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
    if REDIS_URL
    else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}

CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not REDIS_URL)
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "delete-expired-files": {"task": "orders.tasks.delete_expired_files", "schedule": crontab(hour=2, minute=30)},
    "release-queued-orders": {"task": "orders.tasks.release_queued_orders", "schedule": 600.0},
    "sweep-pending-payments": {"task": "billing.tasks.sweep_pending_payments", "schedule": 300.0},
}

EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "SUBRA <no-reply@localhost>")
ADMIN_ALERT_EMAIL = env("ADMIN_ALERT_EMAIL", "")

# --- product settings -------------------------------------------------------

PLANNER_MODEL = env("PLANNER_MODEL", "claude-sonnet-5")
ESCALATION_MODEL = env("ESCALATION_MODEL", "claude-opus-5-5")
PLANNER_EFFORT = env("PLANNER_EFFORT", "medium")
ESCALATION_EFFORT = env("ESCALATION_EFFORT", "high")
POLISHER_PLANNER = env("POLISHER_PLANNER", "claude")  # "heuristic" for offline development
AI_PRICING = PRICING
AI_DAILY_CAP_USD = float(env("AI_DAILY_CAP_USD", "10"))
USD_KES_RATE = float(env("USD_KES_RATE", "129.20"))
CARD_FX_MARKUP = float(env("CARD_FX_MARKUP", "0.03"))
PAYMENT_FEE_RATE = float(env("PAYMENT_FEE_RATE", "0.045"))  # estimate when the provider reports no fee
FILE_RETENTION_DAYS = int(env("FILE_RETENTION_DAYS", "7"))
# Privacy and terms pages show a "draft" banner until Joe has reviewed them (build plan, section 10).
LEGAL_PAGES_REVIEWED = env_bool("LEGAL_PAGES_REVIEWED", False)
SUPPORT_EMAIL = env("SUPPORT_EMAIL", "support@example.co.ke")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES_MEDIA = 100 * 1024 * 1024  # decks with video or audio (Amendment 1D)
MAX_SLIDES = 50
UPLOAD_RATE_LIMIT = (10, 3600)  # uploads per hour, per user and per IP
PAYMENT_RATE_LIMIT = (5, 600)  # payment attempts per 10 minutes, per user and per IP

# Payments (Amendment 1A): IntaSend at launch; Daraja is a future provider.
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", "intasend")
INTASEND_ENV = env("INTASEND_ENV", "sandbox")  # sandbox | live
INTASEND_PUBLISHABLE_KEY = env("INTASEND_PUBLISHABLE_KEY", "")
INTASEND_SECRET_KEY = env("INTASEND_SECRET_KEY", "")
INTASEND_WEBHOOK_SECRET = env("INTASEND_WEBHOOK_SECRET", "")  # the "challenge" set in the IntaSend dashboard

# --- security ---------------------------------------------------------------

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", "3600"))
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
