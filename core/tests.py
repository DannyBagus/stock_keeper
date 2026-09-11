"""Tests for variant grouping: clone action + suggest_groups base name."""
from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from core.admin import ProductAdmin
from core.grouping import base_name
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

    def test_single_letter_size_strips_whole_word_only(self):
        # Regression: size "S"/"L"/"M" used to delete every s/l/m in the name
        # ("Milk Still BH" → "Mi k Sti BH"), splitting groups per size.
        self.assertEqual(base_name(self._p("Milk Still BH, schwarz, S", "S", "schwarz")), "Milk Still BH")
        self.assertEqual(base_name(self._p("Milk Still BH, schwarz, L", "L", "schwarz")), "Milk Still BH")
        self.assertEqual(base_name(self._p("Leonisa high waist", "S", "beige")), "Leonisa high waist")

    def test_size_matches_across_separators(self):
        # Name spells the size differently than the field ("75B" vs "75 B").
        p = self._p("Miss Lovely Still BH Schale, 75B, black", "75 B", "black")
        self.assertEqual(base_name(p), "Miss Lovely Still BH Schale")
        p = self._p("Stützstrumpfhosen Style, Open toe, L Plus, normal, skin, 17.02.01.09.1",
                    "L Plus normal", "Skin")
        self.assertEqual(base_name(p), "Stützstrumpfhosen Style Open toe")


class AutoVariantGroupTests(TestCase):
    """Leere variant_group wird beim Speichern abgeleitet — Operator muss nichts setzen."""

    def setUp(self):
        self.cat = Category.objects.create(name="Still- BH's")
        self.vat = Vat.objects.create(name="Normal", rate=Decimal("8.10"), is_default=True)

    def _p(self, name, size="", color="", **kw):
        return Product.objects.create(
            name=name, size=size, color=color, category=self.cat,
            sales_price=Decimal("1"), cost_price=Decimal("1"), vat=self.vat, **kw,
        )

    def test_new_article_gets_clean_group(self):
        p = self._p("Milk Still BH, schwarz, S", "S", "schwarz")
        self.assertEqual(p.variant_group, "Milk Still BH")

    def test_new_size_joins_existing_group_spelling(self):
        self._p("Zoé Still BH", "M", "Rot", variant_group="Zoé Still BH")
        # Different spelling (no accent, extra attributes) → same shop handle.
        p = self._p("Zoe Still BH, schwarz, XL", "XL", "schwarz")
        self.assertEqual(p.variant_group, "Zoé Still BH")

    def test_explicit_group_is_kept(self):
        p = self._p("Milk, rot, L", "L", "Rot", variant_group="Milk Still BH")
        self.assertEqual(p.variant_group, "Milk Still BH")

    def test_partial_save_without_group_field_does_not_derive(self):
        p = self._p("Milk Still BH, rot, M", "M", "rot")
        Product.objects.filter(pk=p.pk).update(variant_group="")
        p.refresh_from_db()
        p.stock_quantity = 3
        p.save(update_fields=["stock_quantity"])
        p.refresh_from_db()
        self.assertEqual(p.variant_group, "")
