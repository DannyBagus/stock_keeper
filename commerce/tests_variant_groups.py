"""Tests for the daily variant-group check (check_variant_groups)."""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from commerce.management.commands.check_variant_groups import find_issues
from core.models import Category, Product, Vat


@override_settings(WEBSHOP_EXCLUDED_CATEGORIES=['Café'])
class CheckVariantGroupsTests(TestCase):
    def setUp(self):
        self.vat = Vat.objects.create(name='Normal', rate=Decimal('8.10'), is_default=True)
        self.cat = Category.objects.create(name="Still- BH's")
        self.cafe = Category.objects.create(name='Café')

    def _p(self, name, size='', color='', group='', cat=None):
        return Product.objects.create(
            name=name, size=size, color=color, variant_group=group, category=cat or self.cat,
            sales_price=Decimal('1'), cost_price=Decimal('1'), vat=self.vat,
        )

    def _titles(self, **kw):
        return {title.split(' (')[0] for title in find_issues(**kw)}

    def test_clean_catalog_has_no_issues_and_sends_no_mail(self):
        self._p('Milk Still BH', 'S', 'Rot', 'Milk Still BH')
        self._p('Milk Still BH', 'M', 'Rot', 'Milk Still BH')
        call_command('check_variant_groups', stdout=StringIO())
        self.assertEqual(find_issues(), {})
        self.assertEqual(len(mail.outbox), 0)

    def test_group_containing_size_is_reported(self):
        self._p('Serena Still BH 85C', '85 C', 'Schwarz', 'Serena Still BH 85C')
        self.assertIn('Gruppenname enthält Grösse/Farbe', self._titles())

    def test_duplicate_size_color_and_missing_size_are_reported(self):
        self._p('Milk Still BH', 'S', 'Rot', 'Milk Still BH')
        self._p('Milk Still BH', 'S', 'rot', 'Milk Still BH')
        self._p('Milk Still BH', '', 'Rot', 'Milk Still BH')
        titles = self._titles()
        self.assertIn('Gleiche Grösse+Farbe mehrfach in einer Gruppe', titles)
        self.assertIn('Grösse fehlt', titles)

    def test_new_short_named_single_article_is_reported_old_one_not(self):
        for size in ('S', 'M'):
            self._p('Woma Sport Still BH', size, 'Schwarz', 'Woma Sport Still BH')
        self._p('Woman', 'XL', 'Schwarz', 'Woman')
        self.assertIn('Neuer Einzelartikel ähnelt bestehender Gruppe', self._titles())
        # Outside the window (deliberately standalone, reported once) → quiet.
        Product.objects.filter(variant_group='Woman').update(
            created_at=timezone.now() - timedelta(days=3))
        self.assertNotIn('Neuer Einzelartikel ähnelt bestehender Gruppe', self._titles())

    def test_legacy_empty_group_reported_excluded_category_ignored(self):
        p = self._p('Sensyball Noppenball 10cm', '10cm', 'gelb')
        c = self._p('Cappuccino', cat=self.cafe)
        Product.objects.filter(pk__in=[p.pk, c.pk]).update(variant_group='')
        issues = find_issues()
        self.assertEqual(list(issues), ['Ohne Varianten-Gruppe'])
        self.assertEqual(len(issues['Ohne Varianten-Gruppe']), 1)

    def test_mail_sent_when_issues(self):
        self._p('Serena Still BH 85C', '85 C', 'Schwarz', 'Serena Still BH 85C')
        call_command('check_variant_groups', recipients='x@example.com', stdout=StringIO())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['x@example.com'])
        self.assertIn('Serena Still BH 85C', mail.outbox[0].body)
