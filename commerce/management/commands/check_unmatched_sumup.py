"""
Täglicher Abgleich: findet SumUp-Zahlungen ohne zugeordneten Sale in Stock
Keeper und verschickt eine Warn-E-Mail. Erkennt "Zahlung kassiert aber
Kauf nicht gebucht"-Fälle am Tag danach, solange die Erinnerung der
Kassier:innen noch frisch ist.

Cron-Vorschlag (Host, täglich 07:00 Schweizer Zeit):
  0 7 * * * docker exec stock_keeper_web python manage.py check_unmatched_sumup
"""
import logging
from datetime import datetime, timedelta, timezone as tz
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from commerce.models import Sale
from reconciliation.sumup_client import SumUpClient, SumUpAPIError

logger = logging.getLogger(__name__)

DEFAULT_RECIPIENTS = [
    'info@mileja.ch',
    'hebammen@mileja.ch',
    'admin@mileja.ch',
]


class Command(BaseCommand):
    help = (
        "Vergleicht erfolgreiche SumUp-PAYMENT-Transaktionen des Vortags mit "
        "den Stock-Keeper-Sales. Schickt eine E-Mail wenn SumUp-Zahlungen "
        "ohne passenden Sale existieren."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--date', type=str, default=None,
            help='Zu prüfender Tag als YYYY-MM-DD. Default: gestern.',
        )
        parser.add_argument(
            '--recipients', type=str, default=None,
            help='Komma-getrennte E-Mail-Liste. Default: info/hebammen/admin@mileja.ch.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='E-Mail nicht verschicken, nur ausgeben.',
        )

    def handle(self, *args, **opts):
        if opts['date']:
            check_date = datetime.strptime(opts['date'], '%Y-%m-%d').date()
        else:
            check_date = (timezone.now() - timedelta(days=1)).date()

        recipients = (
            [r.strip() for r in opts['recipients'].split(',') if r.strip()]
            if opts['recipients']
            else DEFAULT_RECIPIENTS
        )

        self.stdout.write(f"Prüfe SumUp-Zahlungen für {check_date}…")

        try:
            client = SumUpClient()
            data = client._get('/v0.1/me/transactions/history', params={
                'oldest_time': f'{check_date.isoformat()}T00:00:00Z',
                'newest_time': f'{check_date.isoformat()}T23:59:59Z',
                'limit': 100,
            })
            txns = data.get('items', [])
        except SumUpAPIError as e:
            logger.error("check_unmatched_sumup: SumUp API Fehler: %s", e)
            self.stderr.write(f"SumUp API Fehler: {e}")
            return

        # Nur erfolgreiche PAYMENTs (Refunds/Auth-Only ausschliessen)
        payments = [
            t for t in txns
            if t.get('status') == 'SUCCESSFUL' and t.get('type') == 'PAYMENT'
        ]
        self.stdout.write(f"  {len(payments)} erfolgreiche SumUp-PAYMENTs")

        if not payments:
            return

        tx_codes = {t.get('transaction_code') for t in payments if t.get('transaction_code')}
        matched_codes = set(
            Sale.objects
            .filter(transaction_id__in=tx_codes, status=Sale.Status.COMPLETED)
            .values_list('transaction_id', flat=True)
        )

        unmatched = [t for t in payments if t.get('transaction_code') not in matched_codes]
        self.stdout.write(f"  {len(matched_codes)}/{len(payments)} matched — {len(unmatched)} ohne Sale")

        if not unmatched:
            return

        # Sortiert nach Uhrzeit für die Mail
        unmatched.sort(key=lambda t: t.get('timestamp', ''))
        subject = f"[Stock Keeper] {len(unmatched)} SumUp-Zahlung(en) ohne Sale am {check_date.strftime('%d.%m.%Y')}"
        body_lines = [
            f"Am {check_date.strftime('%d.%m.%Y')} wurde(n) {len(unmatched)} SumUp-Zahlung(en) "
            f"erfolgreich belastet, aber in Stock Keeper ist kein passender Verkauf erfasst.",
            "",
            "Zahlungen:",
        ]
        for t in unmatched:
            ts = t.get('timestamp', '')[:19].replace('T', ' ')
            amount = Decimal(str(t.get('amount', 0)))
            code = t.get('transaction_code', '-')
            body_lines.append(f"  {ts} UTC  |  CHF {amount:>7.2f}  |  {code}")
        body_lines += [
            "",
            "Bitte im Stock Keeper prüfen und — falls die Kundin den Verkauf bestätigt — ",
            "einen passenden Sales-Record nacherfassen.",
            "",
            "Sales-Admin: https://stock-keeper.raowy.ch/admin/commerce/sale/",
            "",
            "(automatisch generiert, check_unmatched_sumup)",
        ]
        body = "\n".join(body_lines)

        if opts['dry_run']:
            self.stdout.write("--- DRY RUN ---")
            self.stdout.write(f"To: {', '.join(recipients)}")
            self.stdout.write(f"Subject: {subject}")
            self.stdout.write(body)
            return

        try:
            send_mail(
                subject=subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=recipients,
                fail_silently=False,
            )
            logger.info("check_unmatched_sumup: Mail an %s (%d Zahlungen)",
                        ', '.join(recipients), len(unmatched))
            self.stdout.write(f"Mail verschickt an: {', '.join(recipients)}")
        except Exception as e:
            logger.exception("check_unmatched_sumup: Mailversand fehlgeschlagen")
            self.stderr.write(f"Mailversand fehlgeschlagen: {e}")
