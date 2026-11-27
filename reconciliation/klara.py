"""
KLARA REST-Client für die Umsatzbuchung eines SumUp-Abgleichs (Engine v2).

Gebucht wird nur nach Freigabe in Stock Keeper (SSO-Login + Berechtigung
reconciliation.post_to_klara) und nur, wenn der Abgleich vollständig aufgeht.

  POST /core/v1/bookings                                 Buchung erfassen
  GET  /core/v1/accounting/accounts/{code}/statement     Kontoblatt (Duplikat-Sperre)

Die Gutschrift (1020 / 1101) bleibt im KLARA-Bankabgleich, weil die API keine
Bankbewegung zuordnen kann. Der Beleg-Upload per API-Key ist KLARA-seitig defekt
(Stand 10.11.2026) → PDF wird in KLARA von Hand angehängt.
"""

import logging
from datetime import timedelta

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class KlaraError(Exception):
    pass


class KlaraClient:
    def __init__(self):
        self.base = getattr(settings, 'KLARA_API_BASE', 'https://api.klara.ch/core/v1').rstrip('/')
        key = getattr(settings, 'KLARA_API_KEY', '')
        if not key:
            raise KlaraError("KLARA_API_KEY ist nicht konfiguriert")
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-KEY': key,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })

    def _request(self, method, path, **kwargs):
        try:
            resp = self.session.request(method, f"{self.base}{path}", timeout=30, **kwargs)
        except requests.RequestException as e:
            raise KlaraError(f"KLARA nicht erreichbar: {e}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get('message') or resp.text
            except ValueError:
                detail = resp.text
            raise KlaraError(f"KLARA HTTP {resp.status_code}: {str(detail)[:500]}")
        return resp.json() if resp.content else {}

    def statement_lines(self, account_code: int, date_from, date_to) -> list[dict]:
        lines, offset = [], 0
        while True:
            data = self._request('GET', f"/accounting/accounts/{account_code}/statement", params={
                'date-from': date_from.isoformat(), 'date-to': date_to.isoformat(),
                'limit': 100, 'offset': offset,
            })
            batch = data.get('lines') or []
            lines.extend(batch)
            if len(batch) < 100:
                return lines
            offset += 100

    def create_booking(self, booking: dict, internal_comment: str) -> int:
        day = f"{booking['date'].isoformat()}T00:00:00Z"
        payload = {
            'documentDate': day,
            'bookingDate': day,
            'internalComment': internal_comment,
            'bookingDetails': [
                {
                    'seq': n,
                    'accountCode': line['account'],
                    'crdrType': line['side'],
                    'amount': float(line['amount']),
                    'description': line['text'],
                    'tags': line['tag'],
                    'bookingTypeCode': 'GENERAL_LEDGER',
                }
                for n, line in enumerate(booking['lines'], start=1)
            ],
        }
        data = self._request('POST', '/bookings', json=payload)
        booking_id = data.get('id') or (data.get('booking') or {}).get('id')
        if not booking_id:
            raise KlaraError(f"KLARA hat keine Buchungs-ID geliefert: {str(data)[:300]}")
        return int(booking_id)


def payout_marker(payout) -> str:
    """Kennung im Buchungstext, z. B. '(PID1871758)'."""
    ref = payout.sumup_reference or ''
    parts = ref.split()
    return f"({parts[1]})" if ref.startswith('SUMUP ') and len(parts) > 1 else ref


def find_existing_bookings(client: KlaraClient, payout, bookings, clearing_account: int) -> list[int]:
    """Bereits in KLARA vorhandene Umsatzbuchungen dieser Auszahlung (Duplikat-Sperre)."""
    if not bookings:
        return []
    marker = payout_marker(payout)
    dates = [b['date'] for b in bookings]
    lines = client.statement_lines(clearing_account, min(dates) - timedelta(days=1),
                                   max(dates) + timedelta(days=1))
    return sorted({l.get('bookingId') for l in lines
                   if marker and marker in (l.get('description') or '')})
