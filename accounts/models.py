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


class Company(models.Model):
    """Who pays: one shared credit balance and at most one live subscription. A person on their own is a company
    of one, created the first time it's needed (subscriptions spec, section 2)."""

    name = models.CharField(max_length=120)
    owner = models.ForeignKey(User, on_delete=models.PROTECT, related_name="owned_companies")
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MEMBER = "member", "Member"

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships")
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="membership")  # one company per person
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.user} ({self.get_role_display().lower()} of {self.company})"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER


def membership_for(user):
    """The user's membership, creating a personal company on first use."""
    from django.db import IntegrityError, transaction

    membership = Membership.objects.select_related("company").filter(user=user).first()
    if membership is not None:
        return membership
    try:
        with transaction.atomic():
            company = Company.objects.create(name=user.email, owner=user)
            return Membership.objects.create(company=company, user=user, role=Membership.Role.OWNER)
    except IntegrityError:  # created by a concurrent request
        return Membership.objects.select_related("company").get(user=user)


def company_for(user):
    return membership_for(user).company


class Invite(models.Model):
    """An emailed invitation to join a company. Only the SHA-256 of the token is stored."""

    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="invites")
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True)
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Invite for {self.email} to {self.company}"

    @property
    def is_open(self):
        from django.utils import timezone

        return self.accepted_at is None and self.revoked_at is None and self.expires_at > timezone.now()
