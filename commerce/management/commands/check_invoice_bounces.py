"""
Stündliches Polling der admin-Inbox per IMAP nach Bounce-Mails für
versandte Rechnungs-PDFs.

Hintergrund: send_invoice_email() in commerce/utils.py setzt einen
X-Stock-Keeper-Sale-Id Header. Wenn der Empfänger-Mailserver die
Mail nachträglich ablehnt (Postfach existiert nicht), kommt ein
NDR (Non-Delivery-Report / Bounce) an admin@mileja.ch zurück. Der
Cron parst diese Bounces, ordnet sie via Header zum Sale zu und
setzt invoice_status=FAILED + invoice_last_error.

Cron (Host, stündlich):
  17 * * * * docker exec stock_keeper_web python manage.py check_invoice_bounces \\
      >> /home/daniel/stock_keeper_cron.log 2>&1
"""
import logging
import re
from datetime import datetime, timedelta, timezone as tz

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from commerce.models import Sale

logger = logging.getLogger(__name__)

BOUNCE_FROM_HINTS = ('mailer-daemon', 'postmaster@', 'postmaster ')
BOUNCE_SUBJECT_HINTS = (
    'undelivered', 'undeliverable', 'delivery status',
    'mail delivery', 'returned mail', 'failure notice', 'delivery failure',
)

# Header-Name in lowercase (imap-tools normalisiert die Keys)
SALE_ID_HEADER = 'x-stock-keeper-sale-id'

# Verspätungs-Toleranz: ein Bounce, der vor (invoice_sent_at - 60s) datiert,
# gehört zu einer früheren Mail (z.B. wurde inzwischen erfolgreich resent).
LATE_BOUNCE_TOLERANCE = timedelta(seconds=60)

# Wie alt darf ein Bounce maximal sein, damit wir ihn überhaupt prüfen?
# Default 7 Tage — älteres lassen wir einfach SEEN und ignorieren.
MAX_BOUNCE_AGE_DAYS = 7

SALE_ID_SUBJECT_RE = re.compile(r'Rechnung\s+#(\d+)', re.IGNORECASE)
FINAL_RECIPIENT_RE = re.compile(r'Final-Recipient:\s*rfc822;\s*<?([^>\s]+)', re.IGNORECASE)
DIAGNOSTIC_CODE_RE = re.compile(r'Diagnostic-Code:\s*(.+)', re.IGNORECASE)
SMTP_STATUS_RE = re.compile(r'(5\d{2}[ -]\d\.\d\.\d.+?)$', re.MULTILINE)


def _is_bounce(mail) -> bool:
    """Heuristik: From oder Subject lassen auf einen NDR schliessen."""
    from_addr = (mail.from_ or '').lower()
    subject = (mail.subject or '').lower()
    if any(hint in from_addr for hint in BOUNCE_FROM_HINTS):
        return True
    if any(hint in subject for hint in BOUNCE_SUBJECT_HINTS):
        return True
    return False


def _extract_sale_id(mail):
    """Versucht primär den X-Stock-Keeper-Sale-Id Header aus der angehängten
    Original-Mail zu lesen, fällt sonst auf Subject-/Body-Regex zurück."""
    # imap-tools liefert .headers als Dict mit lowercase keys. Bei Bounces
    # ist die Original-Mail meist als rfc822-Attachment dabei — deren
    # Header tauchen NICHT direkt in mail.headers auf. Wir suchen den
    # Header daher pragmatisch im Body-Text.
    body = f"{mail.text or ''}\n{mail.html or ''}"
    header_match = re.search(
        rf'^{SALE_ID_HEADER}:\s*(\d+)',
        body,
        re.IGNORECASE | re.MULTILINE,
    )
    if header_match:
        return int(header_match.group(1))

    # Fallback 1: aus dem Subject ("Re: Rechnung #442 - Mileja GmbH")
    if mail.subject:
        m = SALE_ID_SUBJECT_RE.search(mail.subject)
        if m:
            return int(m.group(1))

    # Fallback 2: aus dem Body (z.B. zitierter Original-Subject)
    m = SALE_ID_SUBJECT_RE.search(body)
    if m:
        return int(m.group(1))

    return None


def _extract_failed_recipient(body: str):
    m = FINAL_RECIPIENT_RE.search(body)
    return m.group(1).strip() if m else None


def _extract_failure_reason(body: str):
    m = DIAGNOSTIC_CODE_RE.search(body)
    if m:
        return m.group(1).strip()[:300]
    m = SMTP_STATUS_RE.search(body)
    if m:
        return m.group(1).strip()[:300]
    return None


class Command(BaseCommand):
    help = (
        "IMAP-Polling auf admin-Inbox: parst Bounce-Mails von versendeten "
        "Rechnungs-PDFs, setzt Sale.invoice_status=FAILED und schickt eine "
        "Sammel-Notification an hebammen@mileja.ch."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Keine Statusänderungen schreiben, kein Mail senden, kein SEEN-Flag setzen. Nur Diagnose.',
        )
        parser.add_argument(
            '--limit', type=int, default=100,
            help='Maximale Anzahl Mails pro Run (Default 100).',
        )
        parser.add_argument(
            '--folder', type=str, default=None,
            help='IMAP-Folder. Default: IMAP_BOUNCE_FOLDER aus Settings (INBOX).',
        )

    def handle(self, *args, **opts):
        try:
            from imap_tools import MailBox, AND, OR
        except ImportError:
            self.stderr.write("imap-tools nicht installiert: pip install imap-tools")
            return

        if not settings.IMAP_PASSWORD:
            self.stderr.write("IMAP_PASSWORD (oder EMAIL_PASSWORD) nicht gesetzt.")
            logger.error("check_invoice_bounces: kein IMAP_PASSWORD konfiguriert")
            return

        folder = opts['folder'] or settings.IMAP_BOUNCE_FOLDER
        dry_run = opts['dry_run']
        cutoff_date = (timezone.now() - timedelta(days=MAX_BOUNCE_AGE_DAYS)).date()

        matched = []   # (sale, recipient, reason, bounce_date)
        unmatched = 0  # Bounces ohne extrahierbare Sale-ID
        skipped = 0    # Sale gefunden, aber Update nicht angewendet (z.B. älter)
        processed = 0

        self.stdout.write(
            f"check_invoice_bounces: connect imap={settings.IMAP_HOST}:{settings.IMAP_PORT} "
            f"user={settings.IMAP_USER} folder={folder} dry_run={dry_run}"
        )

        try:
            with MailBox(settings.IMAP_HOST, port=settings.IMAP_PORT).login(
                settings.IMAP_USER, settings.IMAP_PASSWORD, initial_folder=folder
            ) as mailbox:
                criteria = AND(
                    seen=False,
                    date_gte=cutoff_date,
                )
                mails = list(mailbox.fetch(
                    criteria,
                    limit=opts['limit'],
                    mark_seen=False,
                    bulk=True,
                ))
                self.stdout.write(f"  {len(mails)} ungelesene Mails seit {cutoff_date}")

                seen_uids = []
                for mail in mails:
                    if not _is_bounce(mail):
                        continue
                    processed += 1
                    sale_id = _extract_sale_id(mail)
                    body = f"{mail.text or ''}\n{mail.html or ''}"
                    recipient = _extract_failed_recipient(body) or '(unbekannt)'
                    reason = _extract_failure_reason(body) or '(kein Diagnostic-Code im Bounce)'
                    bounce_date = mail.date or timezone.now()

                    if sale_id is None:
                        unmatched += 1
                        logger.warning(
                            "check_invoice_bounces: Bounce ohne Sale-ID uid=%s subject=%r",
                            mail.uid, (mail.subject or '')[:80],
                        )
                        seen_uids.append(mail.uid)
                        continue

                    sale = Sale.objects.filter(id=sale_id).first()
                    if not sale or sale.payment_method != Sale.PaymentMethod.INVOICE:
                        skipped += 1
                        logger.info(
                            "check_invoice_bounces: Sale #%s nicht INVOICE/nicht gefunden — skip",
                            sale_id,
                        )
                        seen_uids.append(mail.uid)
                        continue

                    # Verspätungs-Schutz: Bounce älter als letztes invoice_sent_at minus Toleranz?
                    if sale.invoice_sent_at and bounce_date < sale.invoice_sent_at - LATE_BOUNCE_TOLERANCE:
                        skipped += 1
                        logger.info(
                            "check_invoice_bounces: Bounce für Sale #%s älter als invoice_sent_at — skip (überschreibt keinen erfolgreichen Resend)",
                            sale_id,
                        )
                        seen_uids.append(mail.uid)
                        continue

                    if sale.invoice_status == Sale.InvoiceStatus.FAILED:
                        # Bereits FAILED — nur loggen, nichts überschreiben (aber SEEN markieren).
                        skipped += 1
                        logger.info(
                            "check_invoice_bounces: Sale #%s bereits FAILED — skip",
                            sale_id,
                        )
                        seen_uids.append(mail.uid)
                        continue

                    bounce_ts = bounce_date.strftime('%Y-%m-%d %H:%M UTC') if bounce_date else '?'
                    err = f"Bounce ({bounce_ts}): {reason} | Empfänger: {recipient}"
                    self.stdout.write(
                        f"  → Sale #{sale.id} ({sale.customer_email}) FAILED: {reason[:80]}"
                    )

                    if not dry_run:
                        sale.invoice_status = Sale.InvoiceStatus.FAILED
                        sale.invoice_last_error = err
                        sale.save(update_fields=['invoice_status', 'invoice_last_error'])

                    matched.append({
                        'sale': sale,
                        'recipient': recipient,
                        'reason': reason,
                        'bounce_date': bounce_date,
                    })
                    seen_uids.append(mail.uid)

                if seen_uids and not dry_run:
                    mailbox.flag(seen_uids, '\\Seen', True)
        except Exception:
            logger.exception("check_invoice_bounces: IMAP-Verarbeitung fehlgeschlagen")
            self.stderr.write("IMAP-Verarbeitung fehlgeschlagen — siehe Logs.")
            return

        logger.info(
            "check_invoice_bounces: processed=%d matched=%d unmatched=%d skipped=%d dry_run=%s",
            processed, len(matched), unmatched, skipped, dry_run,
        )
        self.stdout.write(
            f"check_invoice_bounces: processed={processed} matched={len(matched)} "
            f"unmatched={unmatched} skipped={skipped}"
        )

        if not matched:
            return

        # --- Notification-Mail aufbauen ---
        recipients = list(settings.INVOICE_BOUNCE_NOTIFICATION_RECIPIENTS or [])
        if not recipients:
            self.stdout.write("Keine INVOICE_BOUNCE_NOTIFICATION_RECIPIENTS — Notification übersprungen.")
            return

        base_url = getattr(settings, 'STOCK_KEEPER_BASE_URL', 'https://stock-keeper.mileja.ch').rstrip('/')
        subject = f"[Stock Keeper] {len(matched)} Rechnungs-Bounce(s) erkannt"

        body_lines = [
            f"Es wurden {len(matched)} unzustellbare Rechnungs-Mail(s) erkannt.",
            "",
            "Bitte E-Mail-Adresse(n) prüfen und Rechnung(en) erneut senden:",
            "",
        ]
        for hit in matched:
            s = hit['sale']
            ts = hit['bounce_date'].strftime('%Y-%m-%d %H:%M UTC') if hit['bounce_date'] else '?'
            body_lines += [
                f"  Sale #{s.id} | Kundin: {s.customer_first_name} {s.customer_last_name}",
                f"    Original-Empfänger:  {s.customer_email}",
                f"    Bounce-Empfänger:    {hit['recipient']}",
                f"    Bounce-Zeit:         {ts}",
                f"    Fehler:              {hit['reason']}",
                f"    Erneut senden:       {base_url}/commerce/sale/{s.id}/resend-invoice/",
                "",
            ]
        body_lines += [
            "Status der betroffenen Sales wurde auf 'Versand fehlgeschlagen' gesetzt.",
            "",
            "(automatisch generiert, check_invoice_bounces)",
        ]
        body = "\n".join(body_lines)

        if dry_run:
            self.stdout.write("--- DRY RUN: Notification-Mail ---")
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
                "check_invoice_bounces: Notification an %s (%d Bounces)",
                ', '.join(recipients), len(matched),
            )
            self.stdout.write(f"Notification verschickt an: {', '.join(recipients)}")
        except Exception:
            logger.exception("check_invoice_bounces: Notification-Mailversand fehlgeschlagen")
            self.stderr.write("Notification-Mailversand fehlgeschlagen — siehe Logs.")
