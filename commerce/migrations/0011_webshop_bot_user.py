"""Create the `webshop_bot` user the SupportElle webshop API attributes sales to."""
from django.contrib.auth.hashers import make_password
from django.db import migrations

WEBSHOP_BOT_USERNAME = 'webshop_bot'


def create_webshop_bot(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    User.objects.get_or_create(
        username=WEBSHOP_BOT_USERNAME,
        defaults={
            'is_staff': False,
            'is_superuser': False,
            'is_active': True,
            'first_name': 'SupportElle',
            'last_name': 'Webshop',
            'email': 'hebammen@mileja.ch',
            # Token-auth only — no password login. make_password(None) => unusable.
            'password': make_password(None),
        },
    )


def remove_webshop_bot(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    User.objects.filter(username=WEBSHOP_BOT_USERNAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('commerce', '0010_alter_sale_payment_method'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(create_webshop_bot, remove_webshop_bot),
    ]
