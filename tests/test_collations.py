#!/usr/bin/env python
#
# Copyright (c), 2022-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest

from elementpath import ElementPathError, ElementPathValueError, \
    XPath1Parser, XPath2Parser, Collation, MatchingCollation, \
    CollationUnit, MatchRange
from elementpath.collations import UNICODE_CODEPOINT_COLLATION, \
    HTML_ASCII_CASE_INSENSITIVE_COLLATION, CollationManager, \
    _iter_minimal_matches


GERMANISH_COLLATION_URI = 'http://example.com/test-collation/germanish'
IGNORED_SEPARATORS = {'-', '\u00b7'}


class GermanishCollation(MatchingCollation):
    """
    Test collation: 'ß' and 'ss' are equivalent, comparisons are case
    insensitive and the characters in IGNORED_SEPARATORS are ignored.
    """
    def iter_units(self, text):
        start = 0
        for char in text:
            end = start + len(char)
            if char in IGNORED_SEPARATORS:
                yield CollationUnit(start, end, ())
            elif char == 'ß':
                # One source character expanding into two comparison units
                yield CollationUnit(start, end, ('s', 's'))
            else:
                yield CollationUnit(start, end, (char.casefold(),))
            start = end


class CompareOnlyTestCollation(Collation):
    """A collation that can compare strings but provides no matching."""
    def sort_key(self, text):
        return tuple(text.casefold())


def make_parser(collation_uri=GERMANISH_COLLATION_URI, collation=None):
    parser = XPath2Parser()
    parser.register_collation(collation_uri, collation or GermanishCollation())
    return parser


def make_manager(parser, collation_uri=GERMANISH_COLLATION_URI):
    # Any function token provides the link to the parser/static context
    token = parser.parse("contains('a', 'b')")
    return CollationManager(collation_uri, token=token)


class CollationsTest(unittest.TestCase):

    def test_context_manager_init(self):
        manager = CollationManager(collation=UNICODE_CODEPOINT_COLLATION)
        self.assertIsInstance(manager, CollationManager)

        with self.assertRaises(ElementPathError) as ctx:
            CollationManager(collation=None)

        self.assertIn('XPTY0004', str(ctx.exception))
        self.assertIn('collation cannot be an empty sequence', str(ctx.exception))

        # Not raised in __init__()
        manager = CollationManager(collation='unknown')
        self.assertIsInstance(manager, CollationManager)

    def test_context_activation(self):
        with CollationManager(UNICODE_CODEPOINT_COLLATION) as manager:
            self.assertFalse(manager.eq('a', 'A'))
        self.assertIsInstance(manager, CollationManager)

        with self.assertRaises(ElementPathError) as ctx:
            with CollationManager(collation='unknown'):
                pass

        self.assertIn('FOCH0002', str(ctx.exception))
        self.assertIn("Unsupported collation 'unknown'", str(ctx.exception))

    def test_html_ascii_case_insensitive_collation(self):
        with CollationManager(HTML_ASCII_CASE_INSENSITIVE_COLLATION) as manager:
            self.assertTrue(manager.eq('a', 'A'))

    #
    # Protocol / sort keys
    #
    def test_collation_units_protocol(self):
        collation = GermanishCollation()
        units = collation.units('a-ß')
        self.assertEqual(units, [
            CollationUnit(0, 1, ('a',)),
            CollationUnit(1, 2, ()),                       # ignored separator
            CollationUnit(2, 3, ('s', 's')),              # one char, two units
        ])
        self.assertEqual(collation.sort_key('Straße'),
                         collation.sort_key('STRASSE'))
        # Offsets refer to the original (non-folded) string
        self.assertEqual([u.start for u in collation.units('ß')], [0])

    def test_minimal_matches_do_not_split_source_units(self):
        collation = GermanishCollation()
        haystack = collation.units('saßs')
        # 'ss' matches the whole 'ß' unit
        self.assertEqual(_iter_minimal_matches(haystack, collation.units('ss')),
                         [MatchRange(2, 3)])
        # A single 's' cannot match half of the expanded 'ß' unit
        self.assertEqual(_iter_minimal_matches(haystack, collation.units('s')),
                         [MatchRange(0, 1), MatchRange(3, 4)])

    #
    # Compare-only collation: equality works, substring matching is refused
    #
    def test_compare_only_collation(self):
        parser = make_parser('http://example.com/cmp-only', CompareOnlyTestCollation())
        token = parser.parse("compare('a', 'b')")
        with CollationManager('http://example.com/cmp-only', token=token) as manager:
            self.assertTrue(manager.eq('A', 'a'))
            self.assertEqual(manager.strcoll('A', 'a'), 0)
            self.assertFalse(manager.supports_matching)
            for method in (manager.contains, manager.startswith, manager.endswith,
                           manager.match_range):
                with self.assertRaises(ElementPathError) as ctx:
                    method('Abc', 'a')
                self.assertIn('FOCH0002', str(ctx.exception))

    #
    # Registration scope
    #
    def test_registration_is_local_to_parser(self):
        parser = make_parser()
        self.assertIs(parser.collations[GERMANISH_COLLATION_URI].__class__,
                      GermanishCollation)

        other = XPath2Parser()
        self.assertNotIn(GERMANISH_COLLATION_URI, other.collations)

        # A different parser falls back to locale resolution -> unsupported
        token = other.parse("contains('a', 'b')")
        with self.assertRaises(ElementPathError):
            with CollationManager(GERMANISH_COLLATION_URI, token=token):
                pass

    def test_registration_validation(self):
        parser = XPath1Parser()
        with self.assertRaises(ElementPathValueError):
            parser.register_collation('', GermanishCollation())
        with self.assertRaises(ElementPathValueError):
            parser.register_collation(GERMANISH_COLLATION_URI, object())  # type: ignore[arg-type]

    def test_custom_collation_does_not_touch_locale(self):
        import locale
        before = locale.setlocale(locale.LC_COLLATE)
        parser = make_parser()
        with make_manager(parser) as manager:
            manager.contains('Straße', 'ss')
        self.assertEqual(locale.setlocale(locale.LC_COLLATE), before)

    #
    # Matching semantics of the Germanish test collation (boundaries)
    #
    def test_compare_equivalence(self):
        with make_manager(make_parser()) as manager:
            self.assertEqual(manager.strcoll('Strasse', 'Straße'), 0)
            self.assertEqual(manager.strcoll('Straße', 'STRASSE'), 0)
            self.assertEqual(manager.strcoll('Stra--ße', 'strasse'), 0)
            self.assertLess(manager.strcoll('a', 'b'), 0)
            self.assertGreater(manager.strcoll('b', 'a'), 0)

    def test_contains(self):
        with make_manager(make_parser()) as manager:
            self.assertTrue(manager.contains('Straße', 'ss'))
            self.assertTrue(manager.contains('xß', 'ss'))
            self.assertTrue(manager.contains('Str--asse', 'ra'))
            self.assertTrue(manager.contains('Straße', 's'))
            # A source collation unit can never be split
            self.assertFalse(manager.contains('s', 'ß'))
            self.assertFalse(manager.contains('Straße', 'xy'))
            self.assertTrue(manager.contains('abc', ''))

    def test_starts_with(self):
        with make_manager(make_parser()) as manager:
            self.assertTrue(manager.startswith('Straße', 'strass'))
            # Ignored leading units are allowed by the spec
            self.assertTrue(manager.startswith('-Straße', 'str'))
            self.assertFalse(manager.startswith('x-Straße', 'str'))

    def test_ends_with(self):
        with make_manager(make_parser()) as manager:
            self.assertTrue(manager.endswith('Straße', 'sse'))
            # Ignored trailing units are allowed by the spec
            self.assertTrue(manager.endswith('Stra-', 'a'))
            self.assertFalse(manager.endswith('Stra-x', 'a'))

    def test_match_ranges_cut_original_string(self):
        with make_manager(make_parser()) as manager:
            # Ignored chars between matched units belong to the span
            match_range = manager.match_range('Str--asse', 'ra')
            self.assertEqual(match_range, MatchRange(2, 6))

            # The range points into the ORIGINAL string, folded text is
            # never returned to the caller
            text = 'STRAßE'
            before = manager.match_range(text, 'strass')
            after = manager.match_range(text, 'strass', maximal=True)
            self.assertEqual(text[:before.start], '')
            self.assertEqual(text[after.end:], 'E')

            # substring-before: minimal match (no trailing ignored unit)
            self.assertEqual(manager.match_range('a-bc', 'a'),
                             MatchRange(0, 1))
            # substring-after: maximal match (trailing ignored units included)
            self.assertEqual(manager.match_range('a-bc', 'a', maximal=True),
                             MatchRange(0, 2))
            self.assertEqual(manager.match_range('x--', 'x', maximal=True),
                             MatchRange(0, 3))
            self.assertIsNone(manager.match_range('Straße', 'xyz'))

    def test_empty_substring(self):
        with make_manager(make_parser()) as manager:
            self.assertTrue(manager.contains('abc', ''))
            self.assertTrue(manager.startswith('abc', ''))
            self.assertTrue(manager.endswith('abc', ''))
            self.assertEqual(manager.match_range('abc', ''), MatchRange(0, 0))
            self.assertEqual(manager.match_range('abc', '', maximal=True),
                             MatchRange(0, 0))


class CollationFunctionsThroughXPathTest(unittest.TestCase):
    """End to end checks evaluating real XPath expressions."""

    @classmethod
    def setUpClass(cls):
        cls.parser = make_parser()
        cls.uri = GERMANISH_COLLATION_URI

    def check_value(self, expression, expected):
        result = self.parser.parse(expression).evaluate()
        self.assertEqual(result, expected, expression)

    def test_compare(self):
        self.check_value(f"compare('Strasse', 'Straße', '{self.uri}')", 0)

    def test_contains_and_friends(self):
        self.check_value(f"contains('Straße', 'ss', '{self.uri}')", True)
        self.check_value(f"contains('s', 'ß', '{self.uri}')", False)
        self.check_value(f"starts-with('-Straße', 'str', '{self.uri}')", True)
        self.check_value(f"ends-with('Stra-', 'a', '{self.uri}')", True)

    def test_substring_functions_return_original_text(self):
        self.check_value(f"substring-before('Straße', 'ss', '{self.uri}')", 'Stra')
        self.check_value(f"substring-after('Straße', 'ss', '{self.uri}')", 'e')
        self.check_value(f"substring-before('Str--asse', 'ra', '{self.uri}')", 'St')
        self.check_value(f"substring-after('a-b', 'a', '{self.uri}')", 'b')
        # Folding is used only to find the match; the returned slice keeps the
        # original characters and case
        self.check_value(f"substring-before('STRAßE', 'strass', '{self.uri}')", '')
        self.check_value(f"substring-after('STRAßE', 'strass', '{self.uri}')", 'E')

    def test_compare_only_collation_rejects_matching_functions(self):
        parser = XPath2Parser()
        parser.register_collation('http://example.com/cmp', CompareOnlyTestCollation())
        self.assertEqual(parser.parse(
            "compare('A', 'a', 'http://example.com/cmp')").evaluate(), 0)
        for name in ('contains', 'starts-with', 'ends-with',
                     'substring-before', 'substring-after'):
            with self.assertRaises(ElementPathError) as ctx:
                parser.parse(
                    f"{name}('Abc', 'a', 'http://example.com/cmp')").evaluate()
            self.assertIn('FOCH0002', str(ctx.exception), name)

    def test_default_codepoint_collation_unchanged(self):
        self.check_value("compare('Strasse', 'Straße')", -1)
        self.check_value("contains('Straße', 'ss')", False)
        self.check_value("contains('Straße', 'ß')", True)
        self.check_value("starts-with('abc', 'a')", True)
        self.check_value("ends-with('abc', 'BC')", False)
        self.check_value("substring-before('tattoo', 'attoo')", 't')
        self.check_value("substring-after('tattoo', 'tat')", 'too')
        self.check_value("substring-after('abc', '')", 'abc')
        self.check_value("substring-before('abc', '')", '')


if __name__ == '__main__':
    unittest.main()
