"""Placeholder numbers from the subscriptions spec, editable in admin (re-derive them from real AI costs)."""

from django.db import migrations

TIER_CREDITS = {"Small": 4, "Standard": 7, "Large": 10}
PLANS = [("solo", "Solo", 2000, 40, 1), ("team", "Team", 5000, 115, 2), ("business", "Business", 12000, 300, 3)]
PACKS = [("10 credits", 10, 500, 1), ("25 credits", 25, 1150, 2)]


def seed(apps, schema_editor):
    PriceTier = apps.get_model("billing", "PriceTier")
    Plan = apps.get_model("billing", "Plan")
    CreditPack = apps.get_model("billing", "CreditPack")
    for name, credits in TIER_CREDITS.items():
        PriceTier.objects.filter(product_type="deck_polish", name=name, credits=0).update(credits=credits)
    for code, name, price, credits, order in PLANS:
        Plan.objects.get_or_create(code=code, defaults={"name": name, "price_kes": price, "monthly_credits": credits,
                                                        "sort_order": order})
    for name, credits, price, order in PACKS:
        CreditPack.objects.get_or_create(name=name, defaults={"credits": credits, "price_kes": price,
                                                              "sort_order": order})


class Migration(migrations.Migration):
    dependencies = [("billing", "0005_subscriptions_and_credits"), ("orders", "0002_seed_themes_and_prices")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
