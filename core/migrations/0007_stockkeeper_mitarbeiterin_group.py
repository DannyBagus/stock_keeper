from django.db import migrations

GROUP_NAME = 'Stockkeeper-Mitarbeiterin'

# (app_label, model, [actions]) — alle Perms, die eine Mitarbeiterin im
# Stock-Keeper-Alltag braucht. Bewusst ohne 'delete' (bleibt Geschäftsführung).
PERM_SPEC = [
    ('auth', 'user', ['view']),  # nötig, damit Jazzmin-Sidebar-Links sichtbar sind
    ('core', 'product', ['view', 'add', 'change']),
    ('core', 'category', ['view', 'add', 'change']),
    ('core', 'supplier', ['view', 'add', 'change']),
    ('core', 'vat', ['view']),
    ('core', 'stockmovement', ['view', 'add', 'change']),
    ('commerce', 'purchaseorder', ['view', 'add', 'change']),
    ('commerce', 'purchaseorderitem', ['view', 'add', 'change']),
    ('commerce', 'sale', ['view', 'add', 'change']),
    ('commerce', 'saleitem', ['view', 'add', 'change']),
    ('reconciliation', 'sumuppayout', ['view', 'change']),
    ('reconciliation', 'reconciliationitem', ['view', 'change']),
]


def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    ContentType = apps.get_model('contenttypes', 'ContentType')

    group, _ = Group.objects.get_or_create(name=GROUP_NAME)

    perms = []
    for app_label, model, actions in PERM_SPEC:
        try:
            ct = ContentType.objects.get(app_label=app_label, model=model)
        except ContentType.DoesNotExist:
            continue
        for action in actions:
            codename = f'{action}_{model}'
            try:
                perms.append(Permission.objects.get(content_type=ct, codename=codename))
            except Permission.DoesNotExist:
                continue
    group.permissions.set(perms)


def remove_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_alter_product_ean_alter_product_track_stock_and_more'),
        ('auth', '0012_alter_user_first_name_max_length'),
        ('contenttypes', '0002_remove_content_type_name'),
        ('commerce', '0001_initial'),
        ('reconciliation', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(create_group, remove_group),
    ]
