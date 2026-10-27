"""Check everything a real card payment needs, before taking one (subscriptions spec, section 11)."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from billing.models import Plan
from billing.providers import ProviderError
from billing.subscriptions import paystack


class Command(BaseCommand):
    help = "Check the settings and Paystack set-up that live card subscriptions need. Exits non-zero on a problem."

    def add_arguments(self, parser):
        parser.add_argument("--test-mode", action="store_true", help="Allow a test key (sk_test_...)")

    def handle(self, *args, test_mode=False, **options):
        provider = paystack()
        problems, notes = [], []

        def check(ok, problem):
            if not ok:
                problems.append(problem)

        check(settings.SUBSCRIPTIONS_ENABLED, "SUBSCRIPTIONS_ENABLED is off")
        check(not settings.DEBUG, "DJANGO_DEBUG is on")
        check(provider.configured, "PAYSTACK_SECRET_KEY is not set")
        if provider.configured and not test_mode:
            check(provider.mode == "live", f"PAYSTACK_SECRET_KEY is a {provider.mode or 'unrecognised'} key, not "
                                           "sk_live_... (use --test-mode to check a test set-up)")
        check(settings.SITE_URL.startswith("https://") and "localhost" not in settings.SITE_URL,
              f"SITE_URL is {settings.SITE_URL}: Paystack sends customers back there, so it must be the public "
              "https address")
        check(bool(settings.EMAIL_HOST), "EMAIL_HOST is not set: renewal-failed, reminder and invite emails need SMTP")
        check(bool(settings.ADMIN_ALERT_EMAIL), "ADMIN_ALERT_EMAIL is not set: payment alerts would go nowhere")
        if settings.RENEWALS_PAUSED:
            notes.append("RENEWALS_PAUSED is on: nothing will lapse or expire")
        if settings.PAYSTACK_STARTER_LIMIT_KES:
            notes.append(f"PAYSTACK_STARTER_LIMIT_KES is {settings.PAYSTACK_STARTER_LIMIT_KES:,}: set it to 0 once "
                         "the business is registered")

        plans = list(Plan.objects.filter(active=True))
        check(bool(plans), "No active plans")
        for plan in plans:
            label = f"{plan.name}{' (staff only)' if plan.staff_only else ''}"
            if not plan.paystack_plan_code or plan.paystack_mode != provider.mode:
                problems.append(f"{label} isn't set up at Paystack for this key: run manage.py sync_paystack_plans")
                continue
            if plan.paystack_amount_kes != plan.price_kes:
                problems.append(f"{label}: price changed since the last sync: run manage.py sync_paystack_plans")
                continue
            if not provider.configured:
                continue
            try:
                remote = provider.fetch_plan(plan.paystack_plan_code)
            except ProviderError as e:
                problems.append(f"{label}: Paystack couldn't be reached or doesn't know {plan.paystack_plan_code} ({e})")
                continue
            amount = int(remote.get("amount") or 0) // 100
            before = len(problems)
            check(amount == plan.price_kes, f"{label}: Paystack charges KES {amount}, SUBRA shows KES {plan.price_kes}")
            check(str(remote.get("currency") or "KES") == "KES", f"{label}: Paystack plan isn't in KES")
            if len(problems) == before:
                self.stdout.write(f"ok  {label}: {plan.paystack_plan_code}, KES {amount:,} a month")

        for note in notes:
            self.stdout.write(f"note  {note}")
        self.stdout.write(f"webhook URL to set in the Paystack dashboard ({provider.mode or '?'} mode): "
                          f"{settings.SITE_URL}/billing/webhooks/paystack/")
        if problems:
            for problem in problems:
                self.stdout.write(f"PROBLEM  {problem}")
            raise CommandError(f"{len(problems)} problem(s): don't take real card payments yet")
        self.stdout.write("All checks passed.")
