"""Tests for variant grouping: clone action + suggest_groups base name."""
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from core.admin import ProductAdmin
from core.management.commands.suggest_groups import base_name
from core.models import Category, Product, Vat


def _request():
    req = RequestFactory().post("/")
    setattr(req, "session", {})
    setattr(req, "_messages", FallbackStorage(req))
    return req


class DuplicateAsVariantTests(TestCase):
    def setUp(self):
        self.vat = Vat.objects.create(name="Normal", rate=Decimal("8.10"), is_default=True)
        self.cat = Category.objects.create(name="Stützstrümpfe")
        self.src = Product.objects.create(
            name="Schenkelstrümpfe offene Zehen",
            category=self.cat, size="M", color="schwarz",
            sales_price=Decimal("39.90"), cost_price=Decimal("20.00"),
            stock_quantity=5, vat=self.vat, image="products/schenkel.jpg",
        )
        self.admin = ProductAdmin(Product, AdminSite())

    def test_clone_creates_variant_with_shared_group_and_fresh_ids(self):
        self.admin.duplicate_as_variant(_request(), Product.objects.filter(pk=self.src.pk))
        self.src.refresh_from_db()
        # Source got a group name (fell back to its name).
        self.assertEqual(self.src.variant_group, "Schenkelstrümpfe offene Zehen")
        clone = Product.objects.exclude(pk=self.src.pk).get()
        # Same group -> will collapse into ONE webshop product.
        self.assertEqual(clone.variant_group, self.src.variant_group)
        # Copied: name, image, price.
        self.assertEqual(clone.name, self.src.name)
        self.assertEqual(clone.image.name, "products/schenkel.jpg")
        self.assertEqual(clone.sales_price, Decimal("39.90"))
        # Blanked size/color; operator fills them in.
        self.assertEqual(clone.size, "")
        self.assertEqual(clone.color, "")
        # Fresh, unique SKU + EAN generated on save.
        self.assertTrue(clone.sku and clone.sku != self.src.sku)
        self.assertTrue(clone.ean and clone.ean != self.src.ean)
        self.assertEqual(clone.stock_quantity, 0)

    def test_clone_preserves_existing_group(self):
        self.src.variant_group = "Schenkelstrümpfe offene Zehen"
        self.src.save(update_fields=["variant_group"])
        self.admin.duplicate_as_variant(_request(), Product.objects.filter(pk=self.src.pk))
        clone = Product.objects.exclude(pk=self.src.pk).get()
        self.assertEqual(clone.variant_group, "Schenkelstrümpfe offene Zehen")


class BaseNameTests(TestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="Stützstrümpfe")
        self.vat = Vat.objects.create(name="Normal", rate=Decimal("8.10"), is_default=True)

    def _p(self, name, size="", color=""):
        return Product.objects.create(
            name=name, size=size, color=color, category=self.cat,
            sales_price=Decimal("1"), cost_price=Decimal("1"), vat=self.vat,
        )

    def test_strips_size_color_and_article_number_keeps_toe_type(self):
        p = self._p(
            "Schenkelstrumpf Style closed toe, M Plus Normal, black, 17.02.01.05.1",
            size="M Plus Normal", color="black",
        )
        base = base_name(p)
        # Toe-type stays (part of product identity); size/color/article-nr gone.
        self.assertIn("closed toe", base.lower())
        self.assertNotIn("17.02.01.05.1", base)
        self.assertNotIn("black", base.lower())

    def test_strips_parenthetical_size_color(self):
        p = self._p("Stützstrumpfhosen Style Open toe (m plus, normal skin) 17.02.01.09.1")
        base = base_name(p)
        self.assertNotIn("(", base)
        self.assertNotIn("17.02.01.09.1", base)
        self.assertIn("open toe", base.lower())
