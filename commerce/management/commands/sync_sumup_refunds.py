"""
Spiegelt in SumUp ausgelöste Rückerstattungen automatisch nach Stock Keeper.

Hintergrund:
  Wird eine SumUp-Kartenzahlung rückerstattet, erscheint sie in der
  Transaktionshistorie als eigener REFUND-Eintrag bzw. die ursprüngliche
  PAYMENT-Zeile erhält ein refunded_amount > 0. Der `transaction_code` der
  Original-Zahlung ist stabil und in Stock Keeper als `Sale.transaction_id`
  gespeichert — darüber wird exakt der zugehörige Verkauf gefunden.

Verhalten:
  - VOLL-Rückerstattung (refundeter Betrag ≈ Verkaufs-Brutto):
      → Sale wird storniert (Sale.refund(): Status REFUNDED + Ware zurück ins Lager).
      Idempotent: bereits stornierte Sales werden übersprungen.
  - TEIL-Rückerstattung (refundeter Betrag < Verkaufs-Brutto):
      → wird NICHT automatisch gebucht (braucht fachliche Beurteilung), nur gemeldet.
  - Refund ohne passenden SK-Sale (z. B. Doppelbelastung, die nie gebucht wurde):
      → nur gemeldet.

Eine Zusammenfassung wird per E-Mail verschickt, sobald etwas storniert wurde
oder Fälle zur manuellen Prüfung anstehen.

Cron (Host, täglich 07:15 CH-Zeit):
  15 5,6 * * * /home/daniel/mileja/workbench/run_at_swiss_time.sh 07:15 \
      docker exec stock_keeper_web python manage.py sync_sumup_refunds \
      >> /home/daniel/stock_keeper_cron.log 2>&1
"""
import logging
from datetime import datetime, timedelta, timezone as tz
from decimal import Decimal
from collections import defaultdict

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from commerce.models import Sale
from reconciliation.sumup_client import SumUpClient, SumUpAPIError

logger = logging.getLogger(__name__)

DEFAULT_RECIPIENTS = ['admin@mileja.ch']

# Toleranz beim Vergleich refundeter Betrag ↔ Verkaufs-Brutto (Voll vs. Teil)
AMOUNT_TOLERANCE = Decimal('0.05')


def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
    except Exception:
        return None


class Command(BaseCommand):
    help = (
        "Storniert in Stock Keeper automatisch Verkäufe, deren SumUp-Zahlung "
        "vollständig rückerstattet wurde. Teil-/unverknüpfte Refunds werden gemeldet."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=7,
            help='Anzahl Tage rückwärts, die nach Refunds durchsucht werden. Default: 7.',
        )
        parser.add_argument(
            '--recipients', type=str, default=None,
            help='Komma-getrennte E-Mail-Liste. Default: admin@mileja.ch.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Nichts stornieren und keine Mail senden, nur ausgeben, was passieren würde.',
        )

    def handle(self, *args, **opts):
        days = max(1, opts['days'])
        dry_run = opts['dry_run']
        recipients = (
            [r.strip() for r in opts['recipients'].split(',') if r.strip()]
            if opts['recipients'] else DEFAULT_RECIPIENTS
        )

        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=days)
        self.stdout.write(
            f"Suche SumUp-Refunds {start_date} – {end_date}"
            f"{' [DRY-RUN]' if dry_run else ''}…"
        )

        try:
            client = SumUpClient()
            txns = client.get_transactions_in_window(start_date, end_date)
        except SumUpAPIError as e:
            logger.error("sync_sumup_refunds: SumUp API Fehler: %s", e)
            self.stderr.write(f"SumUp API Fehler: {e}")
            return

        # Refunds aus der Historie sammeln (REFUND-Einträge + PAYMENT.refunded_amount)
        refund_events = defaultdict(list)   # code -> [REFUND-Einträge]
        payment_info = {}                   # code -> {'amount', 'refunded_amount', 'ts'}
        for t in txns:
            code = t.get('transaction_code')
            if not code:
                continue
            if t.get('type') == 'REFUND':
                refund_events[code].append(t)
            elif t.get('type') == 'PAYMENT':
                ra = t.get('refunded_amount')
                if ra:
                    payment_info[code] = {
                        'amount': Decimal(str(t.get('amount', 0))),
                        'refunded_amount': Decimal(str(ra)),
                        'ts': t.get('timestamp', ''),
                    }

        refunded_codes = set(refund_events) | set(payment_info)
        self.stdout.write(f"  {len(refunded_codes)} rückerstattete Transaktion(en) im Fenster")

        stornoed = []        # (sale, code, betrag)
        partials = []        # (sale, code, refundeter_betrag, brutto)
        unlinked = []        # (code, refundeter_betrag, ts)
        already = []         # (sale, code)
        errors = []          # (code, fehlertext)

        for code in sorted(refunded_codes):
            # Refundeter Gesamtbetrag + Original-Betrag bestimmen
            if code in payment_info:
                refunded_total = payment_info[code]['refunded_amount']
                original_amount = payment_info[code]['amount']
                refund_ts = payment_info[code]['ts']
            else:
                refunded_total = sum(
                    (Decimal(str(e.get('amount', 0))) for e in refund_events[code]), Decimal('0')
                )
                original_amount = None
                refund_ts = max((e.get('timestamp', '') for e in refund_events[code]), default='')

            sale = (
                Sale.objects.filter(transaction_id=code).order_by('-id').first()
            )

            if sale is None:
                unlinked.append((code, refunded_total, refund_ts))
                continue

            if sale.status == Sale.Status.REFUNDED:
                already.append((sale, code))
                continue

            gross = Decimal(str(sale.total_amount_gross))
            reference = original_amount if original_amount is not None else gross
            is_full = refunded_total >= (reference - AMOUNT_TOLERANCE)

            if not is_full:
                partials.append((sale, code, refunded_total, gross))
                continue

            # Voll-Rückerstattung → Storno
            if dry_run:
                stornoed.append((sale, code, gross))
                continue
            try:
                sale.refund(user=None)
                stornoed.append((sale, code, gross))
                logger.info("sync_sumup_refunds: Sale #%s storniert (Refund %s)", sale.id, code)
            except Exception as e:  # pragma: no cover - defensiv
                logger.exception("sync_sumup_refunds: Storno Sale #%s fehlgeschlagen", sale.id)
                errors.append((code, f"Sale #{sale.id}: {e}"))

        self.stdout.write(
            f"  storniert: {len(stornoed)} | teil: {len(partials)} | "
            f"ohne SK-Sale: {len(unlinked)} | bereits storniert: {len(already)} | "
            f"Fehler: {len(errors)}"
        )

        # Mail nur bei handlungsrelevantem Inhalt (nicht bei reinen "bereits storniert")
        if not (stornoed or partials or unlinked or errors):
            return
        if dry_run:
            self._print_report(stornoed, partials, unlinked, errors)
            return

        self._send_report(recipients, stornoed, partials, unlinked, errors)

    # ---- Reporting ----
    def _build_body(self, stornoed, partials, unlinked, errors):
        lines = ["Automatischer Abgleich SumUp-Rückerstattungen → Stock Keeper.", ""]
        if stornoed:
            lines += [f"✓ {len(stornoed)} Verkauf/Verkäufe automatisch storniert "
                      f"(Voll-Rückerstattung, Ware zurück ins Lager):", ""]
            for sale, code, betrag in stornoed:
                lines.append(f"   Sale #{sale.id}  |  CHF {betrag:>8.2f}  |  {code}")
            lines.append("")
        if partials:
            lines += [f"⚠ {len(partials)} Teil-Rückerstattung(en) — NICHT automatisch gebucht, "
                      f"bitte manuell prüfen:", ""]
            for sale, code, refunded, gross in partials:
                lines.append(
                    f"   Sale #{sale.id}  |  Brutto CHF {gross:>8.2f}  |  "
                    f"refundet CHF {refunded:>8.2f}  |  {code}"
                )
            lines.append("")
        if unlinked:
            lines += [f"ℹ {len(unlinked)} Rückerstattung(en) ohne passenden SK-Sale "
                      f"(z. B. Doppelbelastung, die nie gebucht wurde) — nur zur Info:", ""]
            for code, refunded, ts in unlinked:
                ts_disp = (ts or '')[:19].replace('T', ' ')
                lines.append(f"   {ts_disp} UTC  |  CHF {refunded:>8.2f}  |  {code}")
            lines.append("")
        if errors:
            lines += [f"✗ {len(errors)} Fehler beim Storno:", ""]
            for code, msg in errors:
                lines.append(f"   {code}: {msg}")
            lines.append("")
        lines += [
            "Sales-Admin: https://stock-keeper.mileja.ch/admin/commerce/sale/",
            "",
            "(automatisch generiert, sync_sumup_refunds)",
        ]
        return "\n".join(lines)

    def _print_report(self, *groups):
        self.stdout.write(self._build_body(*groups))

    def _send_report(self, recipients, stornoed, partials, unlinked, errors):
        parts = []
        if stornoed:
            parts.append(f"{len(stornoed)} storniert")
        if partials:
            parts.append(f"{len(partials)} Teil")
        if unlinked:
            parts.append(f"{len(unlinked)} ohne Sale")
        if errors:
            parts.append(f"{len(errors)} Fehler")
        subject = f"[Stock Keeper] SumUp-Refunds: {', '.join(parts)}"
        body = self._build_body(stornoed, partials, unlinked, errors)
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'admin@mileja.ch')
        try:
            send_mail(subject, body, from_email, recipients, fail_silently=False)
            self.stdout.write(f"  Mail versendet an {', '.join(recipients)}")
        except Exception as e:  # pragma: no cover
            logger.exception("sync_sumup_refunds: Mailversand fehlgeschlagen")
            self.stderr.write(f"Mailversand fehlgeschlagen: {e}")
