"""Tests for the SupportElle webshop API (auth, export, sales, invoice)."""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from core.models import Category, Product, Supplier, Vat

TOKEN = 'test-webshop-token'
AUTH = {'HTTP_AUTHORIZATION': f'Token {TOKEN}'}


@override_settings(WEBSHOP_API_TOKEN=TOKEN, WEBSHOP_EXCLUDED_CATEGORIES=['Cafe', 'Café', 'CAFE'])
class WebshopApiTests(TestCase):
    def setUp(self):
        self.vat = Vat.objects.create(name='Normal', rate=Decimal('8.10'), is_default=True)
        self.cat_bh = Category.objects.create(name="Still- BH's")
        self.cat_cafe = Category.objects.create(name='Cafe')
        self.supplier = Supplier.objects.create(name='Anita')
        self.p1 = Product.objects.create(
            name='Anita Still-BH Clara', category=self.cat_bh, supplier=self.supplier,
            size='80 B', color='schwarz', sales_price=Decimal('64.90'),
            cost_price=Decimal('30.00'), stock_quantity=5, vat=self.vat,
        )
        self.p2 = Product.objects.create(
            name='Anita Still-BH Clara', category=self.cat_bh, supplier=self.supplier,
            size='85 C', color='schwarz', sales_price=Decimal('64.90'),
            cost_price=Decimal('30.00'), stock_quantity=2, vat=self.vat,
        )
        self.cafe = Product.objects.create(
            name='Cappuccino', category=self.cat_cafe, sales_price=Decimal('5.50'),
            cost_price=Decimal('2.00'), stock_quantity=100, vat=self.vat, track_stock=False,
        )

    # --- auth ---
    def test_requires_token(self):
        self.assertEqual(self.client.get('/api/webshop/products').status_code, 401)
        self.assertEqual(
            self.client.get('/api/webshop/products', HTTP_AUTHORIZATION='Token wrong').status_code, 401
        )

    # --- products ---
    def test_products_export_excludes_cafe(self):
        resp = self.client.get('/api/webshop/products', **AUTH)
        self.assertEqual(resp.status_code, 200)
        skus = {p['sku'] for p in resp.json()['products']}
        self.assertIn(self.p1.sku, skus)
        self.assertIn(self.p2.sku, skus)
        self.assertNotIn(self.cafe.sku, skus)

    def test_products_payload_shape(self):
        art = next(p for p in self.client.get('/api/webshop/products', **AUTH).json()['products']
                   if p['sku'] == self.p1.sku)
        self.assertEqual(art['name'], 'Anita Still-BH Clara')
        self.assertEqual(art['handle'], 'anita-still-bh-clara')
        self.assertEqual(art['vendor'], 'Anita')
        self.assertEqual(art['size'], '80 B')
        self.assertEqual(art['price'], '64.90')
        self.assertEqual(art['vat_rate'], '8.10')

    # --- stock ---
    def test_stock_export(self):
        rows = self.client.get('/api/webshop/stock', **AUTH).json()['stock']
        by_sku = {r['sku']: r['stock_qty'] for r in rows}
        self.assertEqual(by_sku[self.p1.sku], 5)
        self.assertNotIn(self.cafe.sku, by_sku)

    # --- sales ---
    def _post_sale(self, order_number='SE-2026-0001', qty=2):
        payload = {
            'order_number': order_number,
            'payment_method': 'payrexx',
            'items': [{'sku': self.p1.sku, 'quantity': qty}],
        }
        return self.client.post('/api/webshop/sales', data=json.dumps(payload),
                                content_type='application/json', **AUTH)

    def test_sale_creates_and_deducts_stock(self):
        resp = self._post_sale(qty=2)
        self.assertEqual(resp.status_code, 201)
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.stock_quantity, 3)  # 5 - 2
        self.assertEqual(resp.json()['total_gross'], '129.80')

    def test_sale_attributed_to_webshop_bot(self):
        from commerce.models import Sale
        self._post_sale()
        sale = Sale.objects.get(transaction_id='SE-2026-0001')
        self.assertEqual(sale.created_by.username, 'webshop_bot')
        self.assertEqual(sale.payment_method, Sale.PaymentMethod.PAYREXX)

    def test_sale_is_idempotent(self):
        first = self._post_sale(qty=2)
        self.assertEqual(first.status_code, 201)
        second = self._post_sale(qty=2)  # retry same order number
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['idempotent'])
        self.p1.refresh_from_db()
        self.assertEqual(self.p1.stock_quantity, 3)  # NOT decremented twice

    def test_sale_reports_missing_skus(self):
        payload = {'order_number': 'SE-X', 'items': [{'sku': 'DOES-NOT-EXIST', 'quantity': 1}]}
        resp = self.client.post('/api/webshop/sales', data=json.dumps(payload),
                                content_type='application/json', **AUTH)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['missing_skus'], ['DOES-NOT-EXIST'])

    # --- invoices ---
    def test_invoice_404_without_sale(self):
        resp = self.client.post('/api/webshop/invoices', data=json.dumps({'order_number': 'nope'}),
                                content_type='application/json', **AUTH)
        self.assertEqual(resp.status_code, 404)

    @mock.patch('commerce.webshop_api.utils.generate_invoice_pdf', return_value=b'%PDF-FAKE')
    def test_invoice_returns_pdf_and_reference(self, mocked):
        # invoice order first
        payload = {'order_number': 'SE-INV-1', 'payment_method': 'invoice',
                   'customer': {'first_name': 'Anna', 'last_name': 'Muster',
                                'address': 'Weg 1', 'zip_code': '4051', 'city': 'Basel'},
                   'items': [{'sku': self.p1.sku, 'quantity': 1}]}
        self.client.post('/api/webshop/sales', data=json.dumps(payload),
                         content_type='application/json', **AUTH)
        resp = self.client.post('/api/webshop/invoices',
                                data=json.dumps({'order_number': 'SE-INV-1'}),
                                content_type='application/json', **AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['qr_reference'].startswith('Rechnung '))
        import base64
        self.assertEqual(base64.b64decode(body['pdf_base64']), b'%PDF-FAKE')
        self.assertTrue(mocked.called)
