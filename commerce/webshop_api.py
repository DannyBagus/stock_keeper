"""
Token-authenticated API for the SupportElle webshop.

The webshop (separate Django container on the shared `daniel_default` network) is
the customer-facing shop; StockKeeper stays the single source of truth. This
module exposes the minimal surface the webshop needs:

- ``GET  /api/webshop/products``  — full catalog export (CAFE excluded server-side)
- ``GET  /api/webshop/stock``     — lightweight stock delta by SKU
- ``POST /api/webshop/sales``     — book a paid order (creates Sale + SaleItems,
                                    which deduct stock via the model layer),
                                    idempotent on the webshop order number
- ``POST /api/webshop/invoices``  — render the Swiss QR-bill invoice PDF for an
                                    order (wraps ``commerce.utils`` — one PDF
                                    codebase)

Auth: ``Authorization: Token <WEBSHOP_API_TOKEN>`` (shared secret, env-provided).
Sales are attributed to the ``webshop_bot`` user (created by migration 0011).

This deliberately does NOT reuse the Shopify webhook (HMAC + Shopify payload
shape). It reuses the *model* layer (SaleItem.save -> Product.adjust_stock) and
replaces only the HTTP/auth layer. See supportelle/docs/STOCKKEEPER_ANALYSIS.md.
"""
import base64
import functools
import json
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils.crypto import constant_time_compare
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from core.models import Product

from . import utils
from .models import Sale, SaleItem

logger = logging.getLogger(__name__)

WEBSHOP_BOT_USERNAME = 'webshop_bot'


def _excluded_categories():
    return {c.casefold() for c in getattr(settings, 'WEBSHOP_EXCLUDED_CATEGORIES', [])}


def webshop_token_required(view):
    """Guard: constant-time check of the shared bearer token."""

    @csrf_exempt
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        expected = getattr(settings, 'WEBSHOP_API_TOKEN', '')
        header = request.headers.get('Authorization', '')
        provided = header[6:].strip() if header[:6].lower() == 'token ' else ''
        if not expected or not provided or not constant_time_compare(provided, expected):
            return JsonResponse({'error': 'unauthorized'}, status=401)
        return view(request, *args, **kwargs)

    return wrapper


def _webshop_products_qs():
    excluded = _excluded_categories()
    qs = Product.objects.filter(is_active=True).select_related('category', 'supplier', 'vat')
    for p in qs:
        cat_name = p.category.name if p.category else ''
        if cat_name.casefold() in excluded:
            continue
        yield p, cat_name


@webshop_token_required
@require_GET
def products(request):
    """Full catalog: flat articles the webshop groups into products+variants."""
    out = []
    for p, cat_name in _webshop_products_qs():
        out.append({
            'sku': p.sku,
            'name': p.name,
            'handle': slugify(p.name),
            'category': cat_name,
            'vendor': p.supplier.name if p.supplier else '',
            'size': p.size or '',
            'color': p.color or '',
            'price': str(p.sales_price),
            'stock_qty': p.stock_quantity,
            'vat_rate': str(p.vat.rate) if p.vat else '8.10',
            'description_html': p.description or '',
            'is_active': p.is_active,
            'track_stock': p.track_stock,
            # MEDIA-relative path; webshop resolves via mounted media root or URL.
            'images': [p.image.name] if p.image else [],
        })
    return JsonResponse({'products': out})


@webshop_token_required
@require_GET
def stock(request):
    """Lightweight stock delta by SKU."""
    rows = [
        {'sku': p.sku, 'stock_qty': p.stock_quantity, 'updated_at': p.updated_at.isoformat()}
        for p, _ in _webshop_products_qs()
    ]
    return JsonResponse({'stock': rows})


def _get_bot_user():
    User = get_user_model()
    return User.objects.filter(username=WEBSHOP_BOT_USERNAME).first()


def _find_existing_sale(order_number):
    return (
        Sale.objects.filter(idempotency_key=order_number).first()
        or Sale.objects.filter(transaction_id=order_number).first()
    )


PAYMENT_MAP = {
    'payrexx': Sale.PaymentMethod.PAYREXX,
    'twint': Sale.PaymentMethod.TWINT,
    'invoice': Sale.PaymentMethod.INVOICE,
}


@webshop_token_required
@require_POST
def sales(request):
    """
    Book a paid webshop order. Idempotent on ``order_number`` — retries never
    double-decrement stock (the same order returns the existing Sale).

    Payload: {order_number, payment_method, customer{...}?, items:[{sku, quantity,
              unit_price_gross?}]}
    """
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'invalid json'}, status=400)

    order_number = str(data.get('order_number') or '').strip()
    if not order_number:
        return JsonResponse({'error': 'order_number required'}, status=400)
    items = data.get('items') or []
    if not items:
        return JsonResponse({'error': 'items required'}, status=400)

    existing = _find_existing_sale(order_number)
    if existing:
        return JsonResponse({
            'sale_id': existing.id, 'order_number': order_number,
            'idempotent': True, 'total_gross': str(existing.total_amount_gross),
        }, status=200)

    payment_method = PAYMENT_MAP.get(
        str(data.get('payment_method', '')).lower(), Sale.PaymentMethod.PAYREXX
    )
    customer = data.get('customer') or {}
    bot = _get_bot_user()
    missing = []

    try:
        with transaction.atomic():
            sale = Sale.objects.create(
                payment_method=payment_method,
                channel=Sale.SalesChannel.WEB,
                transaction_id=order_number,
                idempotency_key=order_number,
                created_by=bot,
                customer_first_name=customer.get('first_name', ''),
                customer_last_name=customer.get('last_name', ''),
                customer_address=customer.get('address', ''),
                customer_zip_code=customer.get('zip_code', ''),
                customer_city=customer.get('city', ''),
                customer_email=customer.get('email', ''),
            )
            for it in items:
                sku = str(it.get('sku') or '').strip()
                try:
                    qty = int(it.get('quantity') or 0)
                except (TypeError, ValueError):
                    qty = 0
                if not sku or qty <= 0:
                    continue
                product = Product.objects.filter(sku=sku).first()
                if not product:
                    missing.append(sku)
                    continue
                raw_price = it.get('unit_price_gross')
                try:
                    price = Decimal(str(raw_price)) if raw_price is not None else product.sales_price
                except (InvalidOperation, TypeError):
                    price = product.sales_price
                vat_rate = product.vat.rate if product.vat else Decimal('0.00')
                SaleItem.objects.create(
                    sale=sale, product=product, quantity=qty,
                    unit_price_gross=price, vat_rate=vat_rate,
                )
            sale.calculate_totals()
    except IntegrityError:
        # Concurrent duplicate — return the winner.
        existing = _find_existing_sale(order_number)
        if existing:
            return JsonResponse({
                'sale_id': existing.id, 'order_number': order_number, 'idempotent': True,
            }, status=200)
        raise

    if missing:
        logger.warning('webshop sale %s: unknown SKUs %s', order_number, missing)
    return JsonResponse({
        'sale_id': sale.id, 'order_number': order_number,
        'total_gross': str(sale.total_amount_gross), 'missing_skus': missing,
    }, status=201)


@webshop_token_required
@require_POST
def invoices(request):
    """
    Render the Swiss QR-bill invoice PDF for an existing webshop order (created
    via /api/webshop/sales with payment_method=invoice). Wraps commerce.utils so
    the PDF logic lives in exactly one place.

    Payload: {order_number, customer{...}?}
    Returns: {qr_reference, pdf_base64}
    """
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'invalid json'}, status=400)

    order_number = str(data.get('order_number') or '').strip()
    if not order_number:
        return JsonResponse({'error': 'order_number required'}, status=400)

    sale = _find_existing_sale(order_number)
    if not sale:
        return JsonResponse({'error': 'sale not found for order_number'}, status=404)

    customer = data.get('customer')
    if customer:
        sale.customer_first_name = customer.get('first_name', sale.customer_first_name)
        sale.customer_last_name = customer.get('last_name', sale.customer_last_name)
        sale.customer_address = customer.get('address', sale.customer_address)
        sale.customer_zip_code = customer.get('zip_code', sale.customer_zip_code)
        sale.customer_city = customer.get('city', sale.customer_city)
        sale.customer_email = customer.get('email', sale.customer_email)
        sale.save()

    customer_data = sale.customer_data_dict()
    qr_reference = f"Rechnung {sale.id}"  # ref type NON (unstructured)
    pdf_bytes = utils.generate_invoice_pdf(sale, customer_data)

    return JsonResponse({
        'order_number': order_number,
        'sale_id': sale.id,
        'qr_reference': qr_reference,
        'pdf_base64': base64.b64encode(pdf_bytes).decode('ascii'),
    }, status=200)
