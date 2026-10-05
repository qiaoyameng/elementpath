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

from elementpath import ElementPathError, XPath2Parser
from elementpath.collations import UNICODE_CODEPOINT_COLLATION, \
    HTML_ASCII_CASE_INSENSITIVE_COLLATION, CollationManager, \
    AbstractCollation, CollationElement, CodepointCollation, CODEPOINT_COLLATION


SHARP_S_COLLATION_URI = 'http://example.com/collation/sharp-s-test'


class SharpSTestCollation(AbstractCollation):
    """
    Test collation: Latin sharp s (ß/ẞ) is equivalent to 'ss', and the
    middle dot '\u00b7' is an ignored character (produces no collation
    element). ASCII letters match case-insensitively.
    """

    def analyze(self, s):
        elements = []
        for index, ch in enumerate(s):
            end = index + 1
            if ch == '\u00b7':
                continue  # ignored character: no collation element, no span
            elif ch in 'ßẞ':
                # one source character expands into two comparison units
                elements.append(CollationElement('s', index, end))
                elements.append(CollationElement('s', index, end))
            elif ch.isascii() and ch.isalpha():
                elements.append(CollationElement(ch.lower(), index, end))
            else:
                elements.append(CollationElement(ch, index, end))
        return elements


class ComparisonOnlyTestCollation(AbstractCollation):
    """Ordering only: no substring matching semantics."""

    substring_matching = False

    def analyze(self, s):
        return [CollationElement(ch, index, index + 1)
                for index, ch in enumerate(s)]


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

    def test_abstract_collation_protocol(self):
        collation = SharpSTestCollation()

        # Sorting keys
        self.assertEqual(collation.sort_key('Straße'),
                         ('s', 't', 'r', 'a', 's', 's', 'e'))
        self.assertEqual(collation.sort_key('STRASSE'),
                         ('s', 't', 'r', 'a', 's', 's', 'e'))
        # the ignored character generates no comparison unit
        self.assertEqual(collation.sort_key('a·b'), ('a', 'b'))

        # Comparison / equality
        self.assertEqual(collation.compare('Straße', 'strasse'), 0)
        self.assertEqual(collation.compare('Stra·ße', 'STRASSE'), 0)
        self.assertTrue(collation.eq('ß', 'ss'))
        self.assertLess(collation.compare('a', 'b'), 0)
        self.assertGreater(collation.compare('b', 'a'), 0)

    def test_match_ranges_on_original_string(self):
        collation = SharpSTestCollation()

        # match_range returns the span in the ORIGINAL string
        self.assertEqual(collation.match_range('die Straße', 'strasse', 0),
                         (4, 10))
        # the ignored character is inside the matched source span
        self.assertEqual(collation.match_range('Str·aße', 'strasse', 0),
                         (0, 7))
        # empty target: empty span at the matching position
        self.assertEqual(collation.match_range('abc', '', 0), (0, 0))
        self.assertEqual(collation.match_range('abc', '', 3), (3, 3))
        self.assertIsNone(collation.match_range('abc', 'x', 0))

        # contains / starts-with / ends-with
        self.assertTrue(collation.contains('die Straße', 'Strasse'))
        self.assertTrue(collation.contains('ss', 'ß'))
        self.assertTrue(collation.contains('ß', 'ss'))
        self.assertFalse(collation.contains('abc', 'ss'))
        self.assertTrue(collation.starts_with('·straße', 'str'))
        self.assertFalse(collation.starts_with('straße', 'x'))
        self.assertTrue(collation.ends_with('Straße·', 'asse'))
        self.assertFalse(collation.ends_with('Straße', 'assen'))

        # substring functions slice the ORIGINAL string
        self.assertEqual(collation.substring_before('die Straße', 'strasse'),
                         'die ')
        self.assertEqual(collation.substring_after('die Straße', 'die'),
                         ' Straße')
        # separator kept when it precedes the match (it is not part of it)
        self.assertEqual(collation.substring_before('a·ßb', 'ss'), 'a·')
        self.assertEqual(collation.substring_after('a·ßb', 'ss'), 'b')
        self.assertEqual(collation.substring_after('a·ß', 'a'), '·ß')
        # unmatched => empty string, never folded text
        self.assertEqual(collation.substring_before('abc', 'zz'), '')
        self.assertEqual(collation.substring_after('abc', 'zz'), '')
        # empty target boundaries
        self.assertEqual(collation.substring_before('abc', ''), '')
        self.assertEqual(collation.substring_after('abc', ''), 'abc')

    def test_boundaries_of_ignored_and_expansion_chars(self):
        collation = SharpSTestCollation()

        # sharp s at the very start/end and repeated expansions
        self.assertTrue(collation.contains('ß', 'ss'))
        self.assertTrue(collation.contains('ßß', 'ssss'))
        self.assertEqual(collation.match_range('ßß', 'ssss', 0), (0, 2))
        # elements produced by one character share its source span
        elements = collation.analyze('ß')
        self.assertEqual([(e.unit, e.start, e.end) for e in elements],
                         [('s', 0, 1), ('s', 0, 1)])
        # ignored character alone yields no element at all
        self.assertEqual(collation.analyze('···'), [])
        self.assertTrue(collation.contains('···', ''))
        # no element left: the empty match sits at the end of the original string
        self.assertEqual(collation.find('···', ''), 3)
        self.assertEqual(collation.match_range('···', 'a', 0), None)
        # empty target on a fully-ignored argument follows XPath semantics
        self.assertEqual(collation.substring_before('···', ''), '')
        self.assertEqual(collation.substring_after('···', ''), '···')

    def test_codepoint_collation_parity(self):
        collation = CODEPOINT_COLLATION
        self.assertIsInstance(collation, CodepointCollation)

        for a, b in [('abc', 'abc'), ('Straße', 'Strassen'),
                     ('\u0100', '\u00ff'), ('', ''), ('z', 'a')]:
            with CollationManager(UNICODE_CODEPOINT_COLLATION) as manager:
                self.assertEqual(collation.compare(a, b), manager.strcoll(a, b))
                self.assertEqual(collation.contains(a, b),
                                 manager.contains(a, b))
                self.assertEqual(collation.starts_with(a, b),
                                 manager.startswith(a, b))
                self.assertEqual(collation.ends_with(a, b),
                                 manager.endswith(a, b))

    def test_custom_collation_registration_is_parser_scoped(self):
        parser = XPath2Parser(collations={
            SHARP_S_COLLATION_URI: SharpSTestCollation()
        })
        self.assertIn(SHARP_S_COLLATION_URI, parser.collations)

        # no token and no registry argument: not found, locale path is used
        manager = CollationManager(SHARP_S_COLLATION_URI)
        self.assertIsNone(manager.custom_collation)

        with CollationManager(SHARP_S_COLLATION_URI,
                              custom_collations=parser.collations) as manager:
            self.assertIsNotNone(manager.custom_collation)

        def evaluate(expression, p=parser):
            return p.parse(expression).evaluate()

        self.assertEqual(
            evaluate("compare('Straße', 'strasse', '%s')"
                     % SHARP_S_COLLATION_URI), 0
        )
        self.assertTrue(
            evaluate("contains('die Straße', 'Strasse', '%s')"
                     % SHARP_S_COLLATION_URI)
        )

        # another parser does not see the registration (no global state)
        other = XPath2Parser()
        with self.assertRaises(ElementPathError) as ctx:
            other.parse("contains('x', 'x', '%s')"
                        % SHARP_S_COLLATION_URI).evaluate()
        self.assertIn('FOCH0002', str(ctx.exception))

        # runtime register/unregister
        other.register_collation(SHARP_S_COLLATION_URI, SharpSTestCollation())
        self.assertTrue(
            other.parse("contains('ß', 'ss', '%s')"
                        % SHARP_S_COLLATION_URI).evaluate()
        )
        other.unregister_collation(SHARP_S_COLLATION_URI)
        with self.assertRaises(ElementPathError):
            other.parse("contains('ß', 'ss', '%s')"
                        % SHARP_S_COLLATION_URI).evaluate()

        # invalid arguments are rejected
        self.assertRaises(TypeError, parser.register_collation,
                          SHARP_S_COLLATION_URI, object())
        self.assertRaises(ValueError, parser.register_collation,
                          '', SharpSTestCollation())

    def test_comparison_only_collation_rejects_matching_functions(self):
        parser = XPath2Parser(collations={
            'http://example.com/collation/compare-only': ComparisonOnlyTestCollation()
        })

        # comparison is allowed
        self.assertEqual(
            parser.parse("compare('a', 'a', "
                         "'http://example.com/collation/compare-only')").evaluate(),
            0
        )

        for name in ('contains', 'starts-with', 'ends-with',
                     'substring-before', 'substring-after'):
            with self.assertRaises(ElementPathError) as ctx:
                parser.parse(
                    "%s('abc', 'b', "
                    "'http://example.com/collation/compare-only')" % name
                ).evaluate()
            self.assertIn('FOCH0002', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
