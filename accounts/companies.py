"""Company accounts: invites, joining and leaving (subscriptions spec, section 2)."""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import Company, Invite, Membership, User, membership_for


class CompanyError(Exception):
    """Something the customer should see."""


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _owner(user):
    membership = membership_for(user)
    if not membership.is_owner:
        raise CompanyError("Only your company's owner can manage members.")
    return membership.company


def invite_link(token):
    return settings.SITE_URL + reverse("accounts:invite_accept", args=[token])


def create_invite(owner, email):
    """Email an invitation. Returns (invite, token); the raw token exists only in the email."""
    from billing import emails

    if not settings.SUBSCRIPTIONS_ENABLED:
        raise CompanyError("Company accounts aren't available yet.")
    company = _owner(owner)
    email = (email or "").strip().lower()
    if "@" not in email or len(email) > 254:
        raise CompanyError("Enter a valid email address.")
    if company.memberships.filter(user__email__iexact=email).exists():
        raise CompanyError(f"{email} is already a member.")
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        Invite.objects.filter(company=company, email=email, accepted_at__isnull=True, revoked_at__isnull=True).update(
            revoked_at=timezone.now())
        invite = Invite.objects.create(company=company, email=email, token_hash=_hash(token), invited_by=owner,
                                       expires_at=timezone.now() + timedelta(days=settings.INVITE_TTL_DAYS))
        emails.send("invite", email, inviter=owner.email, company=company.name, link=invite_link(token),
                    ttl_days=settings.INVITE_TTL_DAYS)
    return invite, token


def find_invite(token):
    """The open invite for a token, or None (unknown, expired, revoked or already used)."""
    invite = Invite.objects.select_related("company", "invited_by").filter(token_hash=_hash(token or "")).first()
    return invite if invite is not None and invite.is_open else None


def why_cant_join(user, invite):
    """The reason this user can't accept this invite now, or "" if they can."""
    from billing import credits
    from orders.services import unpaid_work

    if user.email.lower() != invite.email.lower():
        return f"This invitation is for {invite.email}. Sign in with that email to accept it."
    current = Membership.objects.select_related("company").filter(user=user).first()
    if current is not None:
        if current.company_id == invite.company_id:
            return "You're already a member of this company."
        if current.is_owner:
            company = current.company
            if credits.live_subscription(company) is not None:
                return "Cancel your own plan first: you can only be in one company."
            if credits.balance(company) > 0:
                return "Your own company still has credits. Use them or ask us to close it first."
            if company.memberships.exclude(user=user).exists():
                return "Your own company has other members. Remove them first: you can only be in one company."
    if unpaid_work(user) is not None:
        return "Pay for or discard your unpaid deck first, so it isn't left without anyone to pay for it."
    return ""


def accept_invite(user, invite):
    reason = why_cant_join(user, invite)
    if reason:
        raise CompanyError(reason)
    with transaction.atomic():
        invite = Invite.objects.select_for_update().get(pk=invite.pk)
        if not invite.is_open:
            raise CompanyError("This invitation has expired or was withdrawn. Ask for a new one.")
        old = Membership.objects.select_related("company").filter(user=user).first()
        if old is not None:
            old_company = old.company
            old.delete()
            if old_company.owner_id == user.pk and not old_company.memberships.exists():
                old_company.archived_at = timezone.now()
                old_company.save(update_fields=["archived_at"])
        membership = Membership.objects.create(company=invite.company, user=user, role=Membership.Role.MEMBER,
                                               invited_by=invite.invited_by)
        invite.accepted_at, invite.accepted_by = timezone.now(), user
        invite.save(update_fields=["accepted_at", "accepted_by"])
    return membership


def revoke_invite(owner, invite_id):
    company = _owner(owner)
    done = Invite.objects.filter(pk=invite_id, company=company, accepted_at__isnull=True,
                                 revoked_at__isnull=True).update(revoked_at=timezone.now())
    if not done:
        raise CompanyError("That invitation can't be withdrawn.")


def remove_member(owner, user_id):
    """The member keeps their login and their decks; they get a personal company on their next visit. Decks they
    uploaded here stay with this company, so the owner can still download what the company paid for."""
    company = _owner(owner)
    if int(user_id) == owner.pk:
        raise CompanyError("You can't remove yourself. Ask us to hand the company to another member.")
    membership = Membership.objects.filter(company=company, user_id=user_id).select_related("user").first()
    if membership is None:
        raise CompanyError("That person isn't a member.")
    membership.delete()
    return membership.user


def members(company):
    return list(company.memberships.select_related("user").order_by("-role", "created_at"))


def open_invites(company):
    return [i for i in company.invites.filter(accepted_at__isnull=True, revoked_at__isnull=True) if i.is_open]


__all__ = ["Company", "CompanyError", "User"]
