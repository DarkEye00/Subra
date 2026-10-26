from django.core.management.base import BaseCommand, CommandError

from billing.providers import ProviderError
from billing.subscriptions import paystack, sync_plans


class Command(BaseCommand):
    help = "Create each SUBRA plan as a Paystack Plan, or update its amount there after a price change."

    def handle(self, *args, **options):
        provider = paystack()
        if not provider.configured:
            raise CommandError("PAYSTACK_SECRET_KEY is not set")
        try:
            results = sync_plans(provider=provider)
        except ProviderError as e:
            raise CommandError(str(e))
        for plan, action in results:
            self.stdout.write(f"{plan.name}: {action} ({plan.paystack_plan_code}, KES {plan.paystack_amount_kes})")
