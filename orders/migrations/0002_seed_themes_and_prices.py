from django.db import migrations

THEMES = [
    ("navy", "Navy Executive", "Deep navy and gold. Classic, boardroom-ready.", 1),
    ("savanna", "Savanna Warm", "Terracotta and ochre on cream. Warm and confident.", 2),
    ("slate", "Clean Slate", "White with emerald accents. Modern and minimal.", 3),
]
TIERS = [("Small", 1, 15, 200), ("Standard", 16, 30, 350), ("Large", 31, 50, 500)]


def seed(apps, schema_editor):
    Theme = apps.get_model("orders", "Theme")
    PriceTier = apps.get_model("billing", "PriceTier")
    for slug, name, description, order in THEMES:
        Theme.objects.get_or_create(slug=slug, defaults={"name": name, "description": description, "sort_order": order})
    for name, lo, hi, price in TIERS:
        PriceTier.objects.get_or_create(
            product_type="deck_polish", name=name, defaults={"min_units": lo, "max_units": hi, "price_kes": price}
        )


class Migration(migrations.Migration):
    dependencies = [("orders", "0001_initial"), ("billing", "0002_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
