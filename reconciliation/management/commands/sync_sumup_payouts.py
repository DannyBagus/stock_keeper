"""
Erkennt neue SumUp-Auszahlungen und legt den Abgleich (Engine v2) automatisch an.

Ohne Argumente: alle Auszahlungen der letzten --days Tage, die noch keinen
Abgleich haben. Mit --reference wird genau eine Auszahlung (neu) abgeglichen —
ein nicht in KLARA gebuchter v1-Abgleich derselben Gutschrift wird ersetzt.

Cron (Host, täglich 07:45 CH-Zeit):
  45 5,6 * * * /home/daniel/mileja/workbench/run_at_swiss_time.sh 07:45 \
      docker exec stock_keeper_web python manage.py sync_sumup_payouts \
      >> /home/daniel/stock_keeper_cron.log 2>&1
"""
from datetime import date, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from reconciliation.models import SumUpPayout
from reconciliation.services import (
    ReconciliationError, create_reconciliation, list_sumup_payouts, sync_new_payouts,
)
from reconciliation.sumup_client import SumUpAPIError, SumUpClient

DEFAULT_RECIPIENTS = ['admin@mileja.ch']
BASE_URL = 'https://stock-keeper.mileja.ch'


class Command(BaseCommand):
    help = "Legt für neue SumUp-Auszahlungen automatisch den Abgleich an und meldet den Status."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=45,
                            help='Auszahlungen der letzten N Tage prüfen (Default 45).')
        parser.add_argument('--reference', help="Genau diese Auszahlung abgleichen, "
                                                "z. B. 'SUMUP PID1871758 PAYOUT 041126'.")
        parser.add_argument('--list', action='store_true', help='Nur Auszahlungen auflisten.')
        parser.add_argument('--recipients', help='Komma-getrennte E-Mail-Liste.')
        parser.add_argument('--no-mail', action='store_true')

    def handle(self, *args, **opts):
        try:
            client = SumUpClient()
            since = timezone.localdate() - timedelta(days=opts['days'])
            if opts['list']:
                for r in list_sumup_payouts(client, since):
                    p = r['payout']
                    state = f"#{p.pk} v{p.engine_version} {p.status}" if p else 'offen'
                    self.stdout.write(f"{r['date']}  {r['amount']:>9}  {r['reference']:<34} {state}")
                return
            if opts['reference']:
                row = next((r for r in list_sumup_payouts(client, date(2025, 1, 1))
                            if r['reference'] == opts['reference']), None)
                if row is None:
                    raise CommandError(f"Auszahlung {opts['reference']} nicht gefunden.")
                created = [create_reconciliation(row['reference'], row['date'], client=client)]
            else:
                created = sync_new_payouts(since, client=client)
        except (SumUpAPIError, ReconciliationError) as e:
            raise CommandError(str(e))

        if not created:
            self.stdout.write(f"[{timezone.now():%Y-%m-%d %H:%M}] Keine neuen SumUp-Auszahlungen.")
            return

        lines = []
        for p in created:
            s = p.booking_summary
            state = 'BEREIT ZUM ABSCHLUSS' if s['is_balanced'] else f"{s['open_count']} offene Position(en)"
            lines.append(
                f"{p.bank_credit_date:%d.%m.%Y}  CHF {p.bank_credit_amount}  ({p.sumup_reference})\n"
                f"  Laden {s['laden_total']} · Café {s['cafe_total']} · Rückerstattungen {s['refunds_total']}"
                f" · Gebühren {s['total_fees']}\n"
                f"  Status: {state}\n"
                + ''.join(f"  ! {w}\n" for w in s['warnings'])
                + ''.join(
                    f"  - {i.get_match_status_display()}: {i.sumup_tx_code or '–'}"
                    f"{f' Sale #{i.sale_id}' if i.sale_id else ''} CHF {i.sumup_amount or i.sk_amount}\n"
                    for i in s['open_items'])
                + f"  {BASE_URL}/reconciliation/{p.pk}/review/\n"
            )
        body = "Neue SumUp-Auszahlung(en) abgeglichen:\n\n" + "\n".join(lines)
        self.stdout.write(body)

        if opts['no_mail']:
            return
        recipients = (opts['recipients'].split(',') if opts['recipients']
                      else getattr(settings, 'SUMUP_RECON_RECIPIENTS', DEFAULT_RECIPIENTS))
        balanced = all(p.booking_summary['is_balanced'] for p in created)
        send_mail(
            subject=("SumUp-Abgleich bereit zur Freigabe" if balanced
                     else "SumUp-Abgleich: offene Positionen"),
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[r.strip() for r in recipients if r.strip()],
        )
