from django.conf import settings


def billing_nav(request):
    """The nav's "Plan & credits" link and balance, for members of companies that have had a plan. One cheap
    lookup; nothing is created here."""
    if not settings.SUBSCRIPTIONS_ENABLED or not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from accounts.models import Membership
    from billing.credits import balance
    from billing.models import Subscription

    membership = Membership.objects.filter(user=request.user).select_related("company").first()
    if membership is None:
        return {}
    company = membership.company
    had_plan = Subscription.objects.filter(company=company).exclude(
        status__in=[Subscription.Status.INCOMPLETE, Subscription.Status.ABANDONED]).exists()
    if not had_plan:
        return {}
    return {"nav_billing": {"balance": balance(company)}}
