"""
Täglicher SumUp-Abgleich in beide Richtungen:

  A) SumUp-Zahlung ohne SK-Sale
     Karte wurde belastet, aber in Stock Keeper ist kein passender Verkauf
     erfasst (Kassier:in hat "Kauf buchen" vergessen oder wurde abgelenkt).

  B) SK-Sale mit SUMUP ohne SumUp-Zahlung
     Im POS wurde SumUp gewählt und der Kauf gebucht, aber bei SumUp
     existiert keine passende Zahlung (Terminal-Abbruch + Force-Book,
     Admin-Miskategorisierung, o.ä.). Führt zu Umsatz-Aufschlüsselung,
     die nicht mit der Bankgutschrift zusammenpasst.

Cron (Host, täglich 07:00 CH-Zeit):
  0 5,6 * * * /home/daniel/mileja/workbench/run_at_swiss_time.sh 07:00 \
      docker exec stock_keeper_web python manage.py check_unmatched_sumup
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

AMOUNT_TOLERANCE = Decimal('0.01')
# Zeitfenster für Heuristik-Match (Sale ohne tx_id). Bewusst grosszügig —
# zwischen SumUp-Zahlung und POS-"Kauf buchen" können einige Minuten liegen,
# wenn die Kassier:in eine Quittung prüft oder kurz abgelenkt ist. Greedy
# nearest-neighbour verhindert Fehl-Matches bei Mehrfachbeträgen am Tag.
TIME_WINDOW_SECONDS = 30 * 60


def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        "Zweiseitiger Abgleich zwischen SumUp-Transaktionen und Stock-Keeper-Sales "
        "für den Vortag. Schickt eine E-Mail, wenn Deltas existieren."
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

        self.stdout.write(f"Prüfe SumUp ↔ Stock Keeper für {check_date}…")

        # --- SumUp Seite ---
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

        payments = [
            t for t in txns
            if t.get('status') == 'SUCCESSFUL' and t.get('type') == 'PAYMENT'
        ]

        # --- SK Seite ---
        sk_sales = list(
            Sale.objects.filter(
                payment_method=Sale.PaymentMethod.SUMUP,
                status=Sale.Status.COMPLETED,
                date__date=check_date,
            ).order_by('date')
        )

        self.stdout.write(f"  {len(payments)} SumUp-PAYMENTs | {len(sk_sales)} SK-Sales (SUMUP, COMPLETED)")

        # --- Schritt 1: Direkt-Match via transaction_id ---
        sumup_codes = {t.get('transaction_code') for t in payments if t.get('transaction_code')}
        sales_matched_ids = set()
        payments_matched_codes = set()
        for s in sk_sales:
            if s.transaction_id and s.transaction_id in sumup_codes:
                sales_matched_ids.add(s.id)
                payments_matched_codes.add(s.transaction_id)

        remaining_payments = [
            t for t in payments
            if t.get('transaction_code') not in payments_matched_codes
        ]
        remaining_sales = [s for s in sk_sales if s.id not in sales_matched_ids]

        # --- Schritt 2: Heuristik-Match — greedy nearest-neighbour auf
        # |Betrag-Differenz| ≤ 0.01 und |Zeit-Differenz| ≤ 30 min.
        # Alle Kandidatenpaare bilden, aufsteigend nach Zeitdifferenz, und
        # solange beide Seiten noch frei sind, matchen. Das vermeidet
        # Fehl-Matches wenn derselbe Betrag mehrfach am Tag vorkommt.
        candidates = []
        for t in remaining_payments:
            ts = _parse_ts(t.get('timestamp'))
            if ts is None:
                continue
            amt = Decimal(str(t.get('amount', 0)))
            code = t.get('transaction_code')
            for s in remaining_sales:
                if not s.date:
                    continue
                if abs(Decimal(str(s.total_amount_gross)) - amt) > AMOUNT_TOLERANCE:
                    continue
                delta = abs((ts - s.date.astimezone(tz.utc)).total_seconds())
                if delta <= TIME_WINDOW_SECONDS:
                    candidates.append((delta, code, s.id))

        candidates.sort(key=lambda x: x[0])
        used_codes = set()
        used_sale_ids = set()
        for delta, code, sale_id in candidates:
            if code in used_codes or sale_id in used_sale_ids:
                continue
            used_codes.add(code)
            used_sale_ids.add(sale_id)

        unmatched_sumup = [
            t for t in remaining_payments
            if t.get('transaction_code') not in used_codes
        ]
        unmatched_sales = [s for s in remaining_sales if s.id not in used_sale_ids]

        self.stdout.write(
            f"  Richtung A (SumUp → SK): {len(unmatched_sumup)} ohne Sale"
        )
        self.stdout.write(
            f"  Richtung B (SK → SumUp): {len(unmatched_sales)} ohne Zahlung"
        )

        if not unmatched_sumup and not unmatched_sales:
            return

        # --- Mail aufbauen ---
        date_fmt = check_date.strftime('%d.%m.%Y')
        counts = []
        if unmatched_sumup:
            counts.append(f"{len(unmatched_sumup)} SumUp→SK")
        if unmatched_sales:
            counts.append(f"{len(unmatched_sales)} SK→SumUp")
        subject = f"[Stock Keeper] SumUp-Abgleich {date_fmt}: {' + '.join(counts)} offen"

        body_lines = [f"Abgleich SumUp ↔ Stock Keeper für {date_fmt}.", ""]

        if unmatched_sumup:
            unmatched_sumup.sort(key=lambda t: t.get('timestamp', ''))
            body_lines += [
                f"A) {len(unmatched_sumup)} SumUp-Zahlung(en) ohne Sale in Stock Keeper",
                "   (Karte belastet, aber kein Verkauf gebucht):",
                "",
            ]
            for t in unmatched_sumup:
                ts_disp = t.get('timestamp', '')[:19].replace('T', ' ')
                amount = Decimal(str(t.get('amount', 0)))
                code = t.get('transaction_code', '-')
                body_lines.append(f"   {ts_disp} UTC  |  CHF {amount:>7.2f}  |  {code}")
            body_lines.append("")

        if unmatched_sales:
            body_lines += [
                f"B) {len(unmatched_sales)} SK-Sale(s) mit Zahlart SumUp ohne passende Zahlung",
                "   (im POS gebucht, bei SumUp nicht vorhanden — Terminal-Abbruch?):",
                "",
            ]
            for s in unmatched_sales:
                ts_disp = s.date.astimezone(tz.utc).strftime('%Y-%m-%d %H:%M:%S')
                amount = Decimal(str(s.total_amount_gross))
                tx = s.transaction_id or '(keine tx_id)'
                body_lines.append(
                    f"   {ts_disp} UTC  |  CHF {amount:>7.2f}  |  Sale #{s.id}  |  {tx}"
                )
            body_lines.append("")

        body_lines += [
            "Was tun?",
            "  A) Fehlende Sales nacherfassen oder beim nächsten Payout via",
            "     Reconciliation-Report einbuchen (ONLY_SUMUP-Zeile).",
            "  B) Sale öffnen, Zahlart prüfen/korrigieren (Bar? Twint?) oder",
            "     stornieren, falls nie bezahlt wurde.",
            "",
            "Sales-Admin: https://stock-keeper.mileja.ch/admin/commerce/sale/",
            "Reconciliation: https://stock-keeper.mileja.ch/reconciliation/",
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
            logger.info(
                "check_unmatched_sumup: Mail an %s (A=%d, B=%d)",
                ', '.join(recipients), len(unmatched_sumup), len(unmatched_sales),
            )
            self.stdout.write(f"Mail verschickt an: {', '.join(recipients)}")
        except Exception as e:
            logger.exception("check_unmatched_sumup: Mailversand fehlgeschlagen")
            self.stderr.write(f"Mailversand fehlgeschlagen: {e}")
