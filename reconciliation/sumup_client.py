"""
SumUp API Client für stock-keeper Reconciliation.

Endpunkte:
  GET /v0.1/me/financials/payouts       — Auszahlungszeilen (start_date, end_date Pflicht)
  GET /v0.1/me/transactions/history     — Transaktionshistorie (paginiert)

Aufbau der Auszahlungsdaten: siehe reconciliation/ledger.py.

Auth: Bearer Token aus settings.SUMUP_API_KEY
"""

import logging
from datetime import date, timedelta

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

SUMUP_BASE = 'https://api.sumup.com'
HISTORY_PATH = '/v0.1/me/transactions/history'
PAYOUTS_PATH = '/v0.1/me/financials/payouts'
PAGE_LIMIT = 100
MAX_PAGES = 200


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
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SumUpAPIError(f"SumUp API Fehler: {e}")

    # ── Auszahlungen ──────────────────────────────────────────────────────

    def get_payout_entries(self, start: date, end: date) -> list[dict]:
        """Alle Auszahlungs-Einträge (PAYOUT + REFUND_DEDUCTION) im Zeitraum."""
        data = self._get(PAYOUTS_PATH, params={
            'start_date': start.isoformat(),
            'end_date': end.isoformat(),
        })
        return data if isinstance(data, list) else data.get('items', [])

    # ── Transaktionen ─────────────────────────────────────────────────────

    def get_transactions_in_window(self, period_start: date, period_end: date) -> list[dict]:
        """
        Alle Transaktionen (PAYMENT/REFUND, jeder Status) im Zeitraum.

        SumUp liefert den next-Link als reinen Query-String (ohne Pfad); er wird
        an den History-Pfad angehängt. Bricht die Paginierung ab, wird ein
        Fehler geworfen statt still eine unvollständige Liste zu liefern.
        """
        data = self._get(HISTORY_PATH, params={
            'oldest_time': period_start.isoformat() + 'T00:00:00Z',
            'newest_time': period_end.isoformat() + 'T23:59:59Z',
            'limit': PAGE_LIMIT,
        })
        items = list(data.get('items', []))
        for _ in range(MAX_PAGES):
            next_link = next((l for l in data.get('links', []) if l.get('rel') == 'next'), None)
            if not next_link or not next_link.get('href'):
                return items
            href = next_link['href']
            path = href if href.startswith('/') else f"{HISTORY_PATH}?{href.lstrip('?')}"
            data = self._get(path)
            items.extend(data.get('items', []))
        raise SumUpAPIError(f"Paginierung nach {MAX_PAGES} Seiten abgebrochen")


def load_ledger_inputs(client: SumUpClient, payout_date: date,
                       history_days: int = 400, txn_days: int = 75):
    """
    Lädt die Rohdaten für die Ledger einer Auszahlung:
      * Auszahlungs-Einträge der letzten `history_days` (für "war schon ausbezahlt")
      * Transaktionen der letzten `txn_days` vor der Auszahlung (Bruttobeträge)
    """
    entries = client.get_payout_entries(payout_date - timedelta(days=history_days),
                                        payout_date + timedelta(days=1))
    txns = client.get_transactions_in_window(payout_date - timedelta(days=txn_days),
                                             payout_date)
    return entries, txns
