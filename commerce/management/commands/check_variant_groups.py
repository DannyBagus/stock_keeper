"""
Tägliche Prüfung der Varianten-Gruppen (Webshop-Gruppierung) mit Mail-Bericht.

`variant_group` wird beim Speichern automatisch abgeleitet (core.grouping). Diese
Prüfung ist das Sicherheitsnetz für Fälle, die die Automatik nicht erkennt:

  1. Artikel ohne Varianten-Gruppe (Altbestand)
  2. Gruppenname enthält Grösse/Farbe eines seiner Artikel
     → im Shop je Grösse/Farbe ein eigenes Produkt statt Auswahl
  3. Neuer Einzelartikel ähnelt einer bestehenden Gruppe (z.B. "Milk" neben
     "Milk Still BH") → gehört vermutlich dorthin. Nur Artikel der letzten
     --hours, sonst meldet jeder bewusst eigenständige Artikel täglich.
  4. Gleiche Grösse+Farbe mehrfach in einer Gruppe → im Shop nur einer wählbar
  5. Artikel ohne Grösse/Farbe in einer Gruppe, deren andere Artikel sie haben
     → im Shop nicht (sauber) auswählbar

Nur Artikel, die der Webshop exportiert (aktiv, ohne ausgeschlossene
Kategorien). Mail nur, wenn es Befunde gibt.

Cron (Host, täglich 07:30 CH-Zeit):
  30 5,6 * * * /home/daniel/mileja/workbench/run_at_swiss_time.sh 07:30 \
      docker exec stock_keeper_web python manage.py check_variant_groups
"""
import logging
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.text import slugify

from commerce.webshop_api import _webshop_products_qs
from core.grouping import clean, strip_token

logger = logging.getLogger(__name__)

DEFAULT_RECIPIENTS = [
    'info@mileja.ch',
    'hebammen@mileja.ch',
    'admin@mileja.ch',
]
ADMIN_URL = 'https://stock-keeper.mileja.ch/admin/core/product/{}/change/'
SIMILARITY = 0.8


def _similar(single: str, other: str) -> bool:
    """Einzelartikel-Gruppe ähnelt einer anderen Gruppe (Kurzname/Tippfehler)."""
    a, b = single.casefold(), other.casefold()
    if b.startswith(a + ' ') or a.startswith(b + ' '):
        return True  # "milk" ↔ "milk still bh"
    return (SequenceMatcher(None, a, b).ratio() >= SIMILARITY
            or SequenceMatcher(None, a, b[:len(a)]).ratio() >= SIMILARITY)  # "woman" ↔ "woma sport…"


def find_issues(hours=25):
    """Befunde je Kategorie: {titel: [zeile, …]}."""
    products = [p for p, _ in _webshop_products_qs()]
    groups = defaultdict(list)
    for p in products:
        if p.variant_group:
            groups[slugify(p.variant_group)].append(p)

    def line(p, extra=''):
        return (f"  {p.sku}  {p.name}  [Gruppe: {p.variant_group or '—'}] {extra}\n"
                f"         {ADMIN_URL.format(p.pk)}")

    issues = defaultdict(list)
    for p in products:
        if not p.variant_group:
            issues['Ohne Varianten-Gruppe'].append(line(p))
        elif any(clean(strip_token(p.variant_group, t)) != clean(p.variant_group)
                 for t in (p.size, p.color) if t):
            issues['Gruppenname enthält Grösse/Farbe (→ je Variante eigenes Shop-Produkt)'].append(
                line(p, f"Grösse={p.size!r} Farbe={p.color!r}"))

    since = timezone.now() - timedelta(hours=hours)
    for key, members in groups.items():
        p = members[0]
        if len(members) == 1 and p.created_at >= since:
            cat = p.category_id
            for other_key, others in groups.items():
                if other_key != key and others[0].category_id == cat and _similar(
                        p.variant_group, others[0].variant_group):
                    issues['Neuer Einzelartikel ähnelt bestehender Gruppe'].append(
                        line(p, f"→ gehört evtl. zu „{others[0].variant_group}“"))
                    break
        if len(members) < 2:
            continue
        seen = defaultdict(list)
        for m in members:
            seen[(m.size.strip().casefold(), m.color.strip().casefold())].append(m)
        for dupes in seen.values():
            if len(dupes) > 1:
                issues['Gleiche Grösse+Farbe mehrfach in einer Gruppe (nur einer wählbar)'].append(
                    '\n'.join(line(m, f"Grösse={m.size!r} Farbe={m.color!r}") for m in dupes))
        for field, label in (('size', 'Grösse'), ('color', 'Farbe')):
            if any(getattr(m, field) for m in members):
                for m in members:
                    if not getattr(m, field):
                        issues[f'{label} fehlt (andere Artikel der Gruppe haben eine)'].append(line(m))
    return issues


class Command(BaseCommand):
    help = "Prüft die Varianten-Gruppen der Webshop-Artikel und mailt Befunde."

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours', type=int, default=25,
            help='Zeitfenster für "neuer Einzelartikel" (Default 25h, täglicher Lauf).',
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
        issues = find_issues(hours=opts['hours'])
        count = sum(len(v) for v in issues.values())
        if not count:
            self.stdout.write("Varianten-Gruppen: keine Befunde.")
            return

        recipients = (
            [r.strip() for r in opts['recipients'].split(',') if r.strip()]
            if opts['recipients']
            else DEFAULT_RECIPIENTS
        )
        subject = f"Webshop: {count} Hinweis(e) zu Varianten-Gruppen"
        body_lines = [
            "Guten Morgen",
            "",
            "Die tägliche Prüfung hat Artikel gefunden, die im Webshop nicht sauber",
            "gruppiert sind. Bitte im Stock Keeper das Feld „Varianten-Gruppe“ (bzw.",
            "Grösse/Farbe) korrigieren — der Shop übernimmt es in der Nacht.",
            "",
        ]
        for title, rows in issues.items():
            body_lines += [f"{title} ({len(rows)}):", *rows, ""]
        body_lines += ["(automatisch generiert, check_variant_groups)"]
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
            logger.info("check_variant_groups: Mail an %s (%d Befunde)", ', '.join(recipients), count)
            self.stdout.write(f"Mail verschickt an: {', '.join(recipients)}")
        except Exception as e:
            logger.exception("check_variant_groups: Mailversand fehlgeschlagen")
            self.stderr.write(f"Mailversand fehlgeschlagen: {e}")
