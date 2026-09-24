from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("An email address is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Customers sign in with email and password."""

    username = None
    email = models.EmailField("email address", unique=True)
    mpesa_phone = models.CharField(max_length=12, blank=True, help_text="Last M-Pesa number used, to prefill checkout")

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []
    objects = UserManager()

    def __str__(self):
        return self.email
