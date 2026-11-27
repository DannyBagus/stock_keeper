"""
Tests SumUp-Abgleich Engine v2.

Die Szenarien bilden reale Fälle aus 2026 nach:
  * Aug-Payout: Teil-Rückerstattung 64.95 auf TAAA4FXAHUG (160.75), Abzug 65.94
    → 64.95 Erlösminderung + 0.99 nicht erstattete Gebühr (alt: Phantom-Sale + Delta 63.53)
  * Juni-Payout: Voll-Refund TAAA232GAYQ vor Auszahlung, 2.19 Gebühr von
    anderer Zeile abgezogen → Gebühr, keine Erlösminderung
  * Sep-Payout: Duplikat (Recon-Nachbuchung zusätzlich zum Kassen-Sale),
    fehlender Sale (9.00)
"""
from datetime import date, datetime, timezone as dt_tz
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, SimpleTestCase

from commerce.models import Sale, SaleItem
from core.models import Category, Product, Vat
from reconciliation.booking import booking_proposal
from reconciliation.ledger import build_ledgers
from reconciliation.matching import run_matching
from reconciliation.models import ReconciliationItem, SumUpPayout
from reconciliation.sumup_client import SumUpClient

REF_JUN = 'SUMUP PID1705253 PAYOUT 030626'
REF_AUG = 'SUMUP PID1771594 PAYOUT 050826'


def payout_row(code, net, fee, day, ref):
    return {'type': 'PAYOUT', 'transaction_code': code, 'amount': net, 'fee': fee,
            'date': day, 'reference': ref, 'status': 'SUCCESSFUL'}


def deduction_row(code, amount, day, refunded):
    return {'type': 'REFUND_DEDUCTION', 'transaction_code': code, 'amount': amount, 'fee': 0.0,
            'date': day, 'reference': refunded, 'status': 'SUCCESSFUL'}


def payment(code, amount, ts, status='SUCCESSFUL'):
    return {'type': 'PAYMENT', 'status': status, 'transaction_code': code,
            'amount': amount, 'timestamp': ts, 'id': f'uuid-{code}'}


class LedgerTests(SimpleTestCase):
    def test_partial_refund_on_own_line_is_revenue_reduction_plus_fee(self):
        entries = [
            payout_row('A', 98.50, 1.50, '2026-08-05', REF_AUG),
            payout_row('HUG', 93.39, 1.42, '2026-08-05', REF_AUG),
            deduction_row('HUG', 64.95, '2026-07-31', 'HUG'),
        ]
        txns = [payment('A', 100.00, '2026-07-10T10:00:00Z'),
                payment('HUG', 160.75, '2026-07-27T13:14:47Z'),
                {'type': 'REFUND', 'status': 'REFUNDED', 'transaction_code': 'HUG',
                 'amount': 64.95, 'timestamp': '2026-07-31T08:06:06Z'}]
        ledger = build_ledgers(entries, txns)[REF_AUG]
        self.assertEqual(ledger.bank_amount, D('191.89'))
        self.assertEqual(ledger.gross_total, D('260.75'))
        self.assertEqual(ledger.refund_total, D('64.95'))
        self.assertEqual(ledger.extra_fee, D('0.99'))
        self.assertEqual(
            ledger.gross_total - ledger.refund_total - ledger.fee_total - ledger.extra_fee,
            ledger.bank_amount,
        )
        self.assertEqual(ledger.warnings, [])

    def test_full_refund_before_payout_is_fee_only(self):
        entries = [
            payout_row('UX', 233.03, 3.58, '2026-06-03', REF_JUN),
            deduction_row('GAYQ', 145.80, '2026-05-17', 'GAYQ'),   # nie ausbezahlt
            deduction_row('UX', 2.19, '2026-05-17', 'GAYQ'),
        ]
        txns = [payment('UX', 238.80, '2026-05-20T09:00:00Z'),
                payment('GAYQ', 145.80, '2026-05-15T08:58:11Z')]
        ledger = build_ledgers(entries, txns)[REF_JUN]
        self.assertEqual(ledger.refund_total, D('0'))
        self.assertEqual(ledger.extra_fee, D('2.19'))
        self.assertEqual(len(ledger.fee_deductions), 1)
        self.assertEqual(ledger.warnings, [])

    def test_refund_of_payment_from_earlier_payout_counts(self):
        entries = [
            payout_row('OLD', 98.50, 1.50, '2026-06-03', REF_JUN),
            payout_row('NEW', 30.00, 1.00, '2026-08-05', REF_AUG),
            deduction_row('NEW', 50.00, '2026-07-15', 'OLD'),
        ]
        txns = [payment('OLD', 100.00, '2026-05-20T09:00:00Z'),
                payment('NEW', 81.00, '2026-07-20T09:00:00Z')]
        ledger = build_ledgers(entries, txns)[REF_AUG]
        self.assertEqual(ledger.refund_total, D('50.00'))
        self.assertEqual(ledger.refunds[0].refunded_code, 'OLD')
        self.assertEqual(ledger.extra_fee, D('0'))

    def test_missing_transaction_is_flagged(self):
        ledger = build_ledgers([payout_row('X', 9.85, 0.15, '2026-08-05', REF_AUG)], [])[REF_AUG]
        self.assertFalse(ledger.is_complete)
        self.assertTrue(ledger.warnings)

    def test_failed_payment_is_ignored(self):
        entries = [payout_row('A', 9.85, 0.15, '2026-08-05', REF_AUG)]
        txns = [payment('A', 10.00, '2026-07-10T10:00:00Z', status='FAILED'),
                payment('A', 10.00, '2026-07-10T10:01:00Z')]
        ledger = build_ledgers(entries, txns)[REF_AUG]
        self.assertEqual(ledger.gross_total, D('10.00'))


class PaginationTests(SimpleTestCase):
    def test_follows_query_string_next_links(self):
        pages = [
            {'items': [{'n': 1}], 'links': [{'rel': 'next', 'href': 'limit=100&oldest_ref=abc'}]},
            {'items': [{'n': 2}], 'links': []},
        ]
        with self.settings(SUMUP_API_KEY='k'):
            client = SumUpClient()
        calls = []

        def fake_get(path, params=None):
            calls.append(path)
            return pages[len(calls) - 1]

        client._get = fake_get
        items = client.get_transactions_in_window(date(2026, 7, 1), date(2026, 7, 31))
        self.assertEqual([i['n'] for i in items], [1, 2])
        self.assertEqual(calls[1], '/v0.1/me/transactions/history?limit=100&oldest_ref=abc')


class MatchingAndBookingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('kasse', is_staff=True)
        vat = Vat.objects.create(name='Normal', rate=D('8.10'), is_default=True)
        laden = Category.objects.create(name='Stützhosen')
        cafe = Category.objects.create(name='Café')
        self.p_laden = Product.objects.create(name='Hose', category=laden, sales_price=D('100'),
                                              cost_price=D('50'), vat=vat, track_stock=False)
        self.p_cafe = Product.objects.create(name='Kaffee', category=cafe, sales_price=D('5'),
                                             cost_price=D('1'), vat=vat, track_stock=False)

    def sale(self, code, amount, product, ts, idem=None):
        s = Sale.objects.create(date=ts, payment_method='SUMUP', transaction_id=code,
                                total_amount_gross=amount, created_by=self.user,
                                idempotency_key=idem)
        SaleItem.objects.create(sale=s, product=product, quantity=1, unit_price_gross=amount)
        return s

    def build(self, entries, txns, ref):
        ledger = build_ledgers(entries, txns)[ref]
        payout = SumUpPayout.objects.create(
            sumup_reference=ref, engine_version=2, status='IN_REVIEW',
            bank_credit_amount=ledger.bank_amount, bank_credit_date=ledger.payout_date,
            refunds_amount=ledger.refund_total, extra_fees_amount=ledger.extra_fee,
            period_start=ledger.period_start, period_end=ledger.period_end,
        )
        codes = {t['transaction_code'] for t in txns if t['type'] == 'PAYMENT'}
        ReconciliationItem.objects.bulk_create(run_matching(payout, ledger, codes))
        return payout

    def test_august_scenario_balances_and_books_refund_as_revenue_reduction(self):
        t1 = datetime(2026, 7, 27, 13, 15, tzinfo=dt_tz.utc)
        t2 = datetime(2026, 7, 31, 23, 30, tzinfo=dt_tz.utc)   # = 01.08. Zürich
        self.sale('HUG', D('160.75'), self.p_laden, t1)
        self.sale('CAF', D('5.00'), self.p_cafe, t2)
        payout = self.build(
            [payout_row('HUG', 93.39, 1.42, '2026-08-05', REF_AUG),
             payout_row('CAF', 4.92, 0.08, '2026-08-05', REF_AUG),
             deduction_row('HUG', 64.95, '2026-07-31', 'HUG')],
            [payment('HUG', 160.75, '2026-07-27T13:14:47Z'),
             payment('CAF', 5.00, '2026-07-31T23:29:00Z')],
            REF_AUG,
        )
        s = payout.booking_summary
        self.assertEqual(s['laden_total'], D('160.75'))
        self.assertEqual(s['cafe_total'], D('5.00'))
        self.assertEqual(s['refunds_total'], D('64.95'))
        self.assertEqual(s['total_fees'], D('2.49'))   # 1.42 + 0.08 + 0.99
        self.assertEqual(s['net_delta'], D('0'))
        self.assertTrue(s['is_balanced'], s['open_items'])
        self.assertEqual(Sale.objects.count(), 2)       # kein Phantom-Sale

        proposal = s['proposal']
        self.assertEqual(proposal['clearing_balance'], D('0'))
        self.assertEqual([b['date'] for b in proposal['bookings']],
                         [date(2026, 7, 31), date(2026, 8, 31)])
        july = proposal['bookings'][0]['lines']
        self.assertIn({'account': 3800, 'side': 'DR', 'amount': D('64.95')},
                      [{k: l[k] for k in ('account', 'side', 'amount')} for l in july])
        for b in proposal['bookings']:
            dr = sum(l['amount'] for l in b['lines'] if l['side'] == 'DR')
            cr = sum(l['amount'] for l in b['lines'] if l['side'] == 'CR')
            self.assertEqual(dr, cr)

    def test_duplicate_detection_prefers_till_sale_over_recon_rebooking(self):
        # SQLite erzwingt den bedingten Unique-Constraint (MySQL nicht) → Sales nur im Speicher
        from reconciliation.ledger import PayoutLine
        from reconciliation.matching import _line_item
        ts = datetime(2026, 8, 27, 13, 43, tzinfo=dt_tz.utc)
        keep = self.sale('DUP', D('5.00'), self.p_cafe, ts)
        dupe = self.sale('OTHER', D('5.00'), self.p_laden, ts, idem='recon-item-725')
        dupe.transaction_id = 'DUP'
        payout = SumUpPayout.objects.create(engine_version=2, bank_credit_amount=D('4.92'),
                                            bank_credit_date=date(2026, 9, 3))
        line = PayoutLine('DUP', D('4.92'), D('0.08'), D('5.00'), ts)
        item = _line_item(payout, line, [dupe, keep])
        self.assertEqual(item.match_status, 'DUPLICATE')
        self.assertEqual(item.sale, keep)
        self.assertEqual(item.channel, 'CAFE')
        self.assertIn(f'#{dupe.pk}', item.resolution_note)

    def test_missing_and_sk_only_are_open_until_resolved(self):
        ts = datetime(2026, 8, 27, 13, 43, tzinfo=dt_tz.utc)
        self.sale('OK', D('5.00'), self.p_cafe, ts)
        orphan = self.sale(None, D('60.00'), self.p_laden, ts)
        payout = self.build(
            [payout_row('OK', 4.92, 0.08, '2026-09-03', 'R'),
             payout_row('MISS', 8.86, 0.14, '2026-09-03', 'R')],
            [payment('OK', 5.00, '2026-08-27T13:43:03Z'),
             payment('MISS', 9.00, '2026-08-20T13:54:44Z')],
            'R',
        )
        items = {i.match_status: i for i in payout.items.all()}
        self.assertEqual(items['ONLY_SK'].sale, orphan)
        self.assertEqual(items['ONLY_SUMUP'].sumup_amount, D('9.00'))
        s = payout.booking_summary
        self.assertEqual(s['net_delta'], D('0'))     # Beträge stimmen trotzdem
        self.assertFalse(s['is_balanced'])
        self.assertEqual(s['open_count'], 2)

        self.client.force_login(get_user_model().objects.create_superuser('chef', 'c@x.ch', 'pw'))
        url = f'/reconciliation/{payout.pk}/item/{{}}/resolve/'
        resp = self.client.post(url.format(items['ONLY_SUMUP'].pk), {'resolution': 'MANUAL'})
        self.assertEqual(resp.status_code, 400)       # Kanal Pflicht
        self.client.post(url.format(items['ONLY_SUMUP'].pk), {'resolution': 'MANUAL', 'channel': 'CAFE'})
        self.client.post(url.format(items['ONLY_SK'].pk),
                         {'resolution': 'PAYMENT_TYPE_CHANGED', 'new_payment_method': 'CASH'})
        orphan.refresh_from_db()
        self.assertEqual(orphan.payment_method, 'CASH')

        s = payout.booking_summary
        self.assertTrue(s['is_balanced'], s['open_items'])
        self.assertEqual(s['cafe_total'], D('14.00'))
        resp = self.client.post(f'/reconciliation/{payout.pk}/complete/')
        self.assertEqual(resp.status_code, 200)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'COMPLETED')
        # Abgeschlossen → keine Änderungen mehr
        resp = self.client.post(url.format(items['ONLY_SK'].pk), {'resolution': 'SALE_DELETED'})
        self.assertEqual(resp.status_code, 400)

    def test_create_sale_links_existing_completed_sale_instead_of_duplicating(self):
        ts = datetime(2026, 8, 27, 13, 43, tzinfo=dt_tz.utc)
        Product.objects.create(name='Diverses', sku='DIVERSES', category=None,
                               sales_price=D('0'), cost_price=D('0'), track_stock=False)
        payout = self.build([payout_row('LATE', 4.92, 0.08, '2026-09-03', 'R')],
                            [payment('LATE', 5.00, '2026-08-27T13:43:03Z')], 'R')
        item = payout.items.get()
        self.assertEqual(item.match_status, 'ONLY_SUMUP')
        late = self.sale('LATE', D('5.00'), self.p_cafe, ts)   # Kasse bucht nachträglich
        self.client.force_login(get_user_model().objects.create_superuser('chef', 'c@x.ch', 'pw'))
        resp = self.client.post(f'/reconciliation/{payout.pk}/item/{item.pk}/create-sale/')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['sale_id'], late.pk)
        self.assertEqual(Sale.objects.filter(transaction_id='LATE').count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.channel, 'CAFE')

    def test_complete_is_blocked_while_open(self):
        payout = self.build([payout_row('MISS', 8.86, 0.14, '2026-09-03', 'R')],
                            [payment('MISS', 9.00, '2026-08-20T13:54:44Z')], 'R')
        self.client.force_login(get_user_model().objects.create_superuser('chef', 'c@x.ch', 'pw'))
        resp = self.client.post(f'/reconciliation/{payout.pk}/complete/')
        self.assertEqual(resp.status_code, 400)
        payout.refresh_from_db()
        self.assertEqual(payout.status, 'IN_REVIEW')

    def test_review_and_pdf_render(self):
        self.sale('A', D('100.00'), self.p_laden, datetime(2026, 7, 10, 10, tzinfo=dt_tz.utc))
        payout = self.build([payout_row('A', 98.50, 1.50, '2026-08-05', REF_AUG)],
                            [payment('A', 100.00, '2026-07-10T10:00:00Z')], REF_AUG)
        self.client.force_login(get_user_model().objects.create_superuser('chef', 'c@x.ch', 'pw'))
        resp = self.client.get(f'/reconciliation/{payout.pk}/review/')
        self.assertContains(resp, 'Buchungsvorschlag KLARA')
        self.assertContains(resp, '1101')
        resp = self.client.get(f'/reconciliation/{payout.pk}/pdf/')
        self.assertEqual(resp['Content-Type'], 'application/pdf')

    def test_bookings_without_proposal_items_use_month_end(self):
        payout = SumUpPayout(engine_version=2, bank_credit_amount=D('0'),
                             bank_credit_date=date(2026, 8, 5), sumup_reference=REF_AUG)
        self.assertEqual(booking_proposal(payout, [])['bookings'], [])

    def test_link_orphan_sale_to_sumup_payment(self):
        ts = datetime(2026, 5, 11, 13, 44, tzinfo=dt_tz.utc)
        orphan = self.sale(None, D('10.00'), self.p_cafe, ts)
        payout = self.build([payout_row('DK2', 9.85, 0.15, '2026-06-03', 'R')],
                            [payment('DK2', 10.00, '2026-05-11T13:56:23Z')], 'R')
        self.client.force_login(get_user_model().objects.create_superuser('chef', 'c@x.ch', 'pw'))
        resp = self.client.get(f'/reconciliation/{payout.pk}/review/')
        self.assertContains(resp, f'Mit #{orphan.pk} verknüpfen')
        item = payout.items.get(match_status='ONLY_SUMUP')
        resp = self.client.post(f'/reconciliation/{payout.pk}/item/{item.pk}/link-sale/',
                                {'sale_id': orphan.pk})
        self.assertEqual(resp.status_code, 200, resp.content)
        orphan.refresh_from_db()
        self.assertEqual(orphan.transaction_id, 'DK2')
        s = payout.booking_summary
        self.assertTrue(s['is_balanced'], s['open_items'])
        self.assertEqual(s['cafe_total'], D('10.00'))


class KlaraPostingTests(TestCase):
    """Buchung in KLARA nach Freigabe in Stock Keeper (HTTP gemockt)."""

    def setUp(self):
        from django.contrib.auth.models import Permission
        vat = Vat.objects.create(name='Normal', rate=D('8.10'), is_default=True)
        cat = Category.objects.create(name='Stützhosen')
        prod = Product.objects.create(name='Hose', category=cat, sales_price=D('100'),
                                      cost_price=D('50'), vat=vat, track_stock=False)
        User = get_user_model()
        self.alessia = User.objects.create_user('alessia', is_staff=True)
        self.alessia.user_permissions.add(Permission.objects.get(codename='post_to_klara'))
        self.kasse = User.objects.create_user('kasse', is_staff=True)
        self.super_kasse = User.objects.create_superuser('nina', 'n@x.ch', 'pw')
        ts = datetime(2026, 10, 10, 10, tzinfo=dt_tz.utc)
        s = Sale.objects.create(date=ts, payment_method='SUMUP', transaction_id='A',
                                total_amount_gross=D('100.00'), created_by=self.kasse)
        SaleItem.objects.create(sale=s, product=prod, quantity=1, unit_price_gross=D('100.00'))
        ledger = build_ledgers([payout_row('A', 98.50, 1.50, '2026-11-04', 'SUMUP PID1871758 PAYOUT 041126')],
                               [payment('A', 100.00, '2026-10-10T10:00:00Z')])['SUMUP PID1871758 PAYOUT 041126']
        self.payout = SumUpPayout.objects.create(
            sumup_reference=ledger.reference, engine_version=2, status='IN_REVIEW',
            bank_credit_amount=ledger.bank_amount, bank_credit_date=ledger.payout_date,
            period_start=ledger.period_start, period_end=ledger.period_end)
        ReconciliationItem.objects.bulk_create(run_matching(self.payout, ledger, {'A'}))
        self.url = f'/reconciliation/{self.payout.pk}/klara/'

    def fake_klara(self, existing=()):
        posted = []

        def request(method, url, **kw):
            resp = mock.Mock(status_code=200, content=b'x')
            if method == 'GET':
                resp.json.return_value = {'lines': [
                    {'bookingId': i, 'description': 'SumUp Abrechnung 10/2026 (PID1871758) – x'}
                    for i in existing]}
            else:
                posted.append(kw['json'])
                resp.json.return_value = {'booking': {'id': 900 + len(posted)}}
            return resp
        return request, posted

    def test_approve_posts_balanced_booking_once(self):
        request, posted = self.fake_klara()
        self.client.force_login(self.alessia)
        with self.settings(KLARA_API_KEY='k'), mock.patch('requests.Session.request', side_effect=request):
            self.assertContains(self.client.get(self.url), 'Freigeben &amp; in KLARA buchen')
            self.client.post(self.url)
            self.client.post(self.url)          # Doppelklick → keine zweite Buchung
        self.assertEqual(len(posted), 1)
        body = posted[0]
        self.assertEqual(body['bookingDate'], '2026-10-31T00:00:00Z')
        lines = {(l['accountCode'], l['crdrType']): l['amount'] for l in body['bookingDetails']}
        self.assertEqual(lines, {(1101, 'DR'): 98.5, (3000, 'CR'): 100.0, (6000, 'DR'): 1.5})
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.klara_booking_ids, '901')
        self.assertEqual(self.payout.status, 'COMPLETED')
        self.assertEqual(self.payout.klara_booked_by, self.alessia)

    def test_existing_klara_booking_blocks_posting(self):
        request, posted = self.fake_klara(existing=[25130])
        self.client.force_login(self.alessia)
        with self.settings(KLARA_API_KEY='k'), mock.patch('requests.Session.request', side_effect=request):
            self.client.post(self.url)
        self.assertEqual(posted, [])
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.klara_booking_ids, '25130')

    def test_without_permission_nothing_is_posted(self):
        request, posted = self.fake_klara()
        self.client.force_login(self.kasse)
        with self.settings(KLARA_API_KEY='k'), mock.patch('requests.Session.request', side_effect=request):
            resp = self.client.get(self.url)
            self.assertNotContains(resp, 'Freigeben &amp; in KLARA buchen</button>')
            self.client.post(self.url)
        self.assertEqual(posted, [])

    def test_superuser_without_explicit_permission_cannot_post(self):
        request, posted = self.fake_klara()
        self.client.force_login(self.super_kasse)
        with self.settings(KLARA_API_KEY='k'), mock.patch('requests.Session.request', side_effect=request):
            self.client.post(self.url)
        self.assertEqual(posted, [])

    def test_klara_error_releases_claim(self):
        def failing(method, url, **kw):
            if method == 'GET':
                return mock.Mock(status_code=200, content=b'x', json=lambda: {'lines': []})
            return mock.Mock(status_code=400, content=b'x', text='bad',
                             json=lambda: {'message': 'bookingDate is required'})
        self.client.force_login(self.alessia)
        with self.settings(KLARA_API_KEY='k'), mock.patch('requests.Session.request', side_effect=failing):
            resp = self.client.post(self.url, follow=True)
        self.assertContains(resp, 'bookingDate is required')
        self.payout.refresh_from_db()
        self.assertEqual(self.payout.klara_booking_ids, '')
        self.assertEqual(self.payout.status, 'IN_REVIEW')

    def test_mail_contains_approval_and_pdf_links(self):
        from django.core import mail
        from django.core.management import call_command
        with mock.patch('reconciliation.management.commands.sync_sumup_payouts.SumUpClient'), \
             mock.patch('reconciliation.management.commands.sync_sumup_payouts.sync_new_payouts',
                        return_value=[self.payout]):
            call_command('sync_sumup_payouts', stdout=mock.Mock())
        self.assertEqual(len(mail.outbox), 1)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(f'/reconciliation/{self.payout.pk}/klara/', html)
        self.assertIn(f'/reconciliation/{self.payout.pk}/pdf/', html)
        self.assertEqual(mail.outbox[0].to, ['info@mileja.ch', 'admin@mileja.ch'])
