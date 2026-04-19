import csv
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from commerce.models import Sale


class Command(BaseCommand):
    help = (
        "Findet verdächtige Doppelbuchungen: Sales mit identischem Betrag, "
        "Zahlungsmethode, Operator und Kanal, die innerhalb eines Zeitfensters "
        "gebucht wurden. Nicht-destruktiv — gibt nur Kandidaten aus."
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90,
                            help='Wie weit zurück suchen (Default: 90 Tage).')
        parser.add_argument('--window-minutes', type=int, default=10,
                            help='Zeitfenster zwischen zwei verdächtigen Sales (Default: 10).')
        parser.add_argument('--payment-method', type=str, default=None,
                            help='Nur eine Zahlungsmethode prüfen (z.B. SUMUP, CASH).')
        parser.add_argument('--csv', type=str, default=None,
                            help='CSV-Ausgabepfad (optional).')

    def handle(self, *args, **opts):
        days = opts['days']
        window = timedelta(minutes=opts['window_minutes'])
        payment_method = opts['payment_method']
        csv_path = opts['csv']

        since = timezone.now() - timedelta(days=days)
        qs = (Sale.objects
              .filter(date__gte=since, status=Sale.Status.COMPLETED)
              .exclude(channel=Sale.SalesChannel.WEB)
              .select_related('created_by')
              .order_by('date'))
        if payment_method:
            qs = qs.filter(payment_method=payment_method)

        groups = defaultdict(list)
        for s in qs:
            key = (s.total_amount_gross, s.payment_method,
                   s.created_by_id, s.channel)
            groups[key].append(s)

        suspects = []
        for key, sales in groups.items():
            if len(sales) < 2:
                continue
            sales.sort(key=lambda x: x.date)
            for prev, curr in zip(sales, sales[1:]):
                delta = curr.date - prev.date
                if delta <= window:
                    suspects.append((prev, curr, delta))

        if not suspects:
            self.stdout.write(self.style.SUCCESS(
                f'Keine Doppelbuchungs-Kandidaten in den letzten {days} Tagen '
                f'(Fenster {opts["window_minutes"]} min) gefunden.'))
            return

        header = ['prev_id', 'curr_id', 'date', 'delta_seconds',
                  'amount_gross', 'payment_method', 'channel', 'operator',
                  'prev_tx_id', 'curr_tx_id']
        rows = []
        for prev, curr, delta in suspects:
            rows.append([
                prev.id, curr.id,
                curr.date.isoformat(), int(delta.total_seconds()),
                str(curr.total_amount_gross), curr.payment_method, curr.channel,
                curr.created_by.username if curr.created_by else '',
                prev.transaction_id or '', curr.transaction_id or '',
            ])

        self.stdout.write(self.style.WARNING(
            f'{len(suspects)} verdächtige Paare gefunden:'))
        self.stdout.write('\t'.join(header))
        for r in rows:
            self.stdout.write('\t'.join(str(x) for x in r))

        if csv_path:
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)
            self.stdout.write(self.style.SUCCESS(f'CSV geschrieben: {csv_path}'))

        sumup_tx_dupes = (Sale.objects
                          .exclude(transaction_id__isnull=True)
                          .exclude(transaction_id='')
                          .values('transaction_id')
                          .annotate(n=Count('id'))
                          .filter(n__gt=1))
        if sumup_tx_dupes.exists():
            self.stdout.write(self.style.ERROR(
                '\nAchtung: Identische transaction_id an mehreren Sales:'))
            for row in sumup_tx_dupes:
                ids = list(Sale.objects
                           .filter(transaction_id=row['transaction_id'])
                           .values_list('id', 'status'))
                self.stdout.write(
                    f"  transaction_id={row['transaction_id']}  "
                    f"count={row['n']}  sales={ids}")
