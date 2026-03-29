"""
SumUp API Client für stock-keeper Reconciliation.

Endpunkte:
  GET /v0.1/me/financials/payouts       — Auszahlungsliste (start_date, end_date Pflicht)
  GET /v0.1/me/transactions/history     — Transaktionshistorie

Payout-Struktur: Jede Transaktion hat ihren eigenen Payout-Eintrag.
Die monatliche Bankgutschrift ist die Summe aller Payout-Amounts eines Auszahlungsdatums.
Mapping: payout.transaction_code == transaction.transaction_code (1:1)

Auth: Bearer Token aus settings.SUMUP_API_KEY
"""

import requests
import logging
from datetime import datetime, date, timedelta
from decimal import Decimal
from django.conf import settings

logger = logging.getLogger(__name__)

SUMUP_BASE = 'https://api.sumup.com'


class SumUpAPIError(Exception):
    pass


class SumUpClient:
    def __init__(self):
        self.api_key = settings.SUMUP_API_KEY
        if not self.api_key:
            raise SumUpAPIError("SUMUP_API_KEY ist nicht konfiguriert")
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        })

    def _get(self, path, params=None):
        url = f"{SUMUP_BASE}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SumUpAPIError(f"SumUp API Fehler: {e}")

    def get_payouts_for_date(self, credit_date: date) -> list[dict]:
        """
        Lädt alle Payouts für ein bestimmtes Auszahlungsdatum.
        SumUp zahlt monatlich aus — alle Payouts eines Monats haben das gleiche Datum.
        """
        # ±1 Tag Toleranz
        start = credit_date - timedelta(days=1)
        end = credit_date + timedelta(days=1)
        data = self._get('/v0.1/me/financials/payouts', params={
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
        })
        # API gibt direkt ein Array zurück
        payouts = data if isinstance(data, list) else data.get('items', [])
        # Nur Payouts am exakten Datum
        return [p for p in payouts if p.get('date') == credit_date.isoformat()]

    def find_payouts_for_credit(self, amount: Decimal, credit_date: date) -> list[dict]:
        """
        Findet die Payouts die zu einer Bankgutschrift passen.
        Sucht in einem ±3 Tage Fenster und prüft ob die Summe der Payouts
        eines Datums dem Gutschriftsbetrag entspricht.
        """
        start = credit_date - timedelta(days=3)
        end = credit_date + timedelta(days=3)
        data = self._get('/v0.1/me/financials/payouts', params={
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
        })
        all_payouts = data if isinstance(data, list) else data.get('items', [])

        # Gruppieren nach Datum
        from collections import defaultdict
        by_date = defaultdict(list)
        for p in all_payouts:
            by_date[p.get('date', '')].append(p)

        # Datum finden dessen Summe zum Betrag passt
        for payout_date, payouts in by_date.items():
            total = sum(Decimal(str(p.get('amount', 0))) for p in payouts)
            if abs(total - amount) <= Decimal('0.10'):
                logger.info(
                    f"Payout-Datum gefunden: {payout_date}, "
                    f"{len(payouts)} Payouts, Summe={total}"
                )
                return payouts

        logger.warning(f"Kein passendes Payout-Datum für CHF {amount} um {credit_date}")
        return []

    def get_transactions_by_codes(self, transaction_codes: set[str],
                                  period_start: date, period_end: date) -> list[dict]:
        """
        Lädt Transaktionen für einen Zeitraum und filtert auf die angegebenen
        transaction_codes. Paginiert bei Bedarf.
        """
        all_transactions = []
        params = {
            'oldest_time': period_start.isoformat() + 'T00:00:00Z',
            'newest_time': period_end.isoformat() + 'T23:59:59Z',
            'limit': 100,
        }

        data = self._get('/v0.1/me/transactions/history', params=params)
        items = data.get('items', [])
        all_transactions.extend(items)

        # Paginierung über links
        while len(items) >= 100 and 'links' in data:
            links = data.get('links', [])
            next_link = next((l for l in links if l.get('rel') == 'next'), None)
            if not next_link:
                break
            next_url = next_link.get('href', '')
            if not next_url:
                break
            try:
                resp = self.session.get(next_url, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                items = data.get('items', [])
                all_transactions.extend(items)
            except requests.RequestException:
                break

        # Filtern auf die relevanten transaction_codes
        matched = [t for t in all_transactions if t.get('transaction_code') in transaction_codes]
        logger.info(
            f"SumUp: {len(all_transactions)} Transaktionen geladen, "
            f"{len(matched)} matched auf Payout-Codes"
        )
        return matched

    def get_payout_transactions(self, payouts: list[dict]) -> tuple[list[dict], date, date]:
        """
        Lädt die Transaktions-Details für eine Liste von Payouts.

        Returns:
            (transactions, period_start, period_end)
        """
        if not payouts:
            return [], None, None

        # Transaction codes aus Payouts
        tx_codes = set(p['transaction_code'] for p in payouts)

        # Geschätzten Zeitraum ermitteln: Payouts des Monats vor dem Auszahlungsdatum
        payout_date = date.fromisoformat(payouts[0]['date'])
        # Transaktionen sind typischerweise vom Vormonat
        period_end = payout_date.replace(day=1) - timedelta(days=1)  # Letzter Tag Vormonat
        period_start = period_end.replace(day=1)  # Erster Tag Vormonat

        # Etwas Puffer geben
        search_start = period_start - timedelta(days=5)
        search_end = period_end + timedelta(days=5)

        transactions = self.get_transactions_by_codes(tx_codes, search_start, search_end)

        # Echte Periode aus Timestamps ableiten
        if transactions:
            timestamps = sorted(
                datetime.fromisoformat(t['timestamp'].replace('Z', '+00:00')).date()
                for t in transactions if t.get('timestamp')
            )
            if timestamps:
                period_start = timestamps[0]
                period_end = timestamps[-1]

        return transactions, period_start, period_end
