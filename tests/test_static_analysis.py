#!/usr/bin/env python
#
# Copyright (c), 2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest
from xml.etree import ElementTree

from elementpath import XPath2Parser, XPathContext, ElementPathError
from elementpath.xpath3 import XPath31Parser
from elementpath.static_analysis import StaticType, StaticAnalysisResult, \
    analyze_expression


class TrackingList(list):
    """A list that records whether it has been iterated."""
    iterated = False

    def __iter__(self):
        type(self).iterated = True
        return super().__iter__()


class StaticTypeTest(unittest.TestCase):

    def test_from_sequence_type(self):
        st = StaticType.from_sequence_type('xs:integer')
        self.assertEqual((st.item_type, st.occurrence), ('xs:integer', (1, 1)))

        st = StaticType.from_sequence_type('xs:string?')
        self.assertEqual((st.item_type, st.occurrence), ('xs:string', (0, 1)))

        st = StaticType.from_sequence_type('node()*')
        self.assertEqual((st.item_type, st.occurrence), ('node()', (0, None)))

        st = StaticType.from_sequence_type('element()+')
        self.assertEqual((st.item_type, st.occurrence), ('element()', (1, None)))

        st = StaticType.from_sequence_type('empty-sequence()')
        self.assertEqual((st.item_type, st.occurrence), ('empty-sequence()', (0, 0)))

        self.assertTrue(StaticType.from_sequence_type('').is_unknown)
        self.assertTrue(StaticType.from_sequence_type(None).is_unknown)

    def test_to_sequence_type(self):
        self.assertEqual(str(StaticType.from_sequence_type('xs:integer')), 'xs:integer')
        self.assertEqual(str(StaticType.from_sequence_type('xs:string?')), 'xs:string?')
        self.assertEqual(str(StaticType.from_sequence_type('node()*')), 'node()*')
        self.assertEqual(str(StaticType.from_sequence_type('element()+')), 'element()+')
        self.assertEqual(str(StaticType.empty_sequence()), 'empty-sequence()')
        self.assertEqual(str(StaticType.unknown()), 'unknown')
        self.assertIsNone(StaticType.unknown().to_sequence_type())

    def test_merge(self):
        st1 = StaticType.from_sequence_type('xs:integer')
        st2 = StaticType.from_sequence_type('xs:string')
        self.assertEqual(str(st1.merge(st2)), 'xs:anyAtomicType')

        st1 = StaticType.from_sequence_type('xs:integer')
        st2 = StaticType.from_sequence_type('xs:integer?')
        self.assertEqual(str(st1.merge(st2)), 'xs:integer?')

        st1 = StaticType.from_sequence_type('element()')
        st2 = StaticType.from_sequence_type('attribute()')
        self.assertEqual(str(st1.merge(st2)), 'node()')

        st1 = StaticType.from_sequence_type('xs:integer')
        st2 = StaticType.from_sequence_type('xs:double')
        self.assertEqual(str(st1.merge(st2)), 'xs:numeric')

        st1 = StaticType.from_sequence_type('xs:integer')
        self.assertTrue(st1.merge(StaticType.unknown()).is_unknown)

    def test_concat(self):
        st1 = StaticType.from_sequence_type('xs:integer')
        st2 = StaticType.from_sequence_type('xs:integer')
        result = st1.concat(st2)
        self.assertEqual((result.min_occurs, result.max_occurs), (2, 2))
        self.assertEqual(str(result), 'xs:integer+')

        st1 = StaticType.from_sequence_type('xs:integer?')
        st2 = StaticType.from_sequence_type('xs:string')
        self.assertEqual(str(st1.concat(st2)), 'xs:anyAtomicType+')

    def test_widened(self):
        st = StaticType.from_sequence_type('xs:string')
        self.assertEqual(str(st.widened()), 'xs:string*')
        self.assertTrue(StaticType.unknown().widened().is_unknown)


class StaticAnalysisErrorsTest(unittest.TestCase):
    """Expressions that are provably wrong for every runtime value."""

    @classmethod
    def setUpClass(cls):
        cls.parser = XPath2Parser(
            variable_types={'x': 'xs:double', 's': 'xs:string', 'b': 'xs:boolean'}
        )

    def check_error(self, source, code='err:XPTY0004'):
        result = analyze_expression(source, self.parser)
        self.assertFalse(result.ok, f'no error detected for {source!r}')
        self.assertTrue(any(e.code == code for e in result.errors),
                        f'no {code} error for {source!r}: {result.errors}')
        return result

    def test_wrong_function_argument_type(self):
        result = self.check_error('fn:abs($s)')
        error = result.errors[0]
        self.assertEqual(error.token.position, (1, 4))
        self.assertIn('xs:string', error.message)
        self.assertIn('xs:numeric?', error.message)

    def test_atomic_value_for_node_parameter(self):
        self.check_error('fn:name(1)')

    def test_wrong_argument_cardinality(self):
        self.check_error('fn:abs((1, 2))')

    def test_empty_sequence_for_required_argument(self):
        self.check_error("fn:string-join(('a', 'b'), ())")

    def test_wrong_arithmetic_operand(self):
        self.check_error("1 + 'x'")
        self.check_error('$b * 2')

    def test_wrong_cast(self):
        self.check_error('(1, 2) cast as xs:integer')
        self.check_error("xs:date('2020-01-01') cast as xs:integer")
        self.check_error('() cast as xs:integer')

    def test_wrong_condition_type(self):
        self.check_error('if ((1, 2)) then 1 else 2', code='err:FORG0006')

    def test_path_on_atomic_value(self):
        self.check_error('1/a', code='err:XPTY0019')

    def test_value_comparison_on_sequence(self):
        self.check_error('1 eq (1, 2)')

    def test_error_position(self):
        result = analyze_expression('fn:abs($s)', self.parser)
        self.assertEqual(result.errors[0].token.position, (1, 4))

        result = analyze_expression('fn:concat(fn:abs($s), "y")', self.parser)
        self.assertEqual(result.errors[0].token.position, (1, 14))

    def test_syntax_error_is_collected(self):
        result = analyze_expression('1 +', self.parser)
        self.assertFalse(result.ok)
        self.assertEqual(result.errors[0].code, 'err:XPST0003')
        self.assertTrue(result.unknown)


class StaticAnalysisValidTest(unittest.TestCase):
    """Expressions that are provably valid, with their inferred sequence type."""

    @classmethod
    def setUpClass(cls):
        cls.parser = XPath2Parser(
            variable_types={'x': 'xs:double', 's': 'xs:string', 'b': 'xs:boolean',
                            'nodes': 'element()*'}
        )

    def check_valid(self, source, sequence_type):
        result = analyze_expression(source, self.parser)
        self.assertTrue(result.ok, f'unexpected errors for {source!r}: {result.errors}')
        self.assertEqual(result.sequence_type, sequence_type,
                         f'wrong type inferred for {source!r}')

    def test_literals(self):
        self.check_valid('1', 'xs:integer')
        self.check_valid('1.5', 'xs:decimal')
        self.check_valid('1e0', 'xs:double')
        self.check_valid("'a'", 'xs:string')

    def test_variables(self):
        self.check_valid('$x', 'xs:double')
        self.check_valid('$s', 'xs:string')

    def test_function_calls(self):
        self.check_valid('fn:abs($x)', 'xs:numeric?')
        self.check_valid('fn:abs(-4)', 'xs:numeric?')
        self.check_valid("fn:concat('a', 'b')", 'xs:string')
        self.check_valid("fn:substring('hello', 1, 3)", 'xs:string')
        self.check_valid("xs:integer('3')", 'xs:integer?')

    def test_numeric_promotion(self):
        self.check_valid('1 + 2', 'xs:integer')
        self.check_valid('1 + 2.5', 'xs:decimal')
        self.check_valid('1 + 1e0', 'xs:double')
        self.check_valid('1 div 2', 'xs:decimal')
        self.check_valid('1 idiv 2', 'xs:integer')
        self.check_valid('2.5 mod 1', 'xs:decimal')

    def test_conditional_expression(self):
        self.check_valid('if ($b) then 1 else 2', 'xs:integer')
        self.check_valid("if ($b) then 1 else 'a'", 'xs:anyAtomicType')
        self.check_valid('if ($b) then 1 else ()', 'xs:integer?')

    def test_path_expressions(self):
        self.check_valid('//a/string()', 'xs:string*')
        self.check_valid('//a/@id', 'attribute()*')
        self.check_valid('$nodes/name()', 'xs:string*')
        self.check_valid('$nodes[1]', 'element()*')

    def test_for_and_quantified_expressions(self):
        self.check_valid('for $i in (1 to 3) return $i * 2', 'xs:integer*')
        self.check_valid('some $i in (1, 2) satisfies $i > 0', 'xs:boolean')
        self.check_valid('every $i in (1, 2) satisfies $i > 0', 'xs:boolean')

    def test_type_expressions(self):
        self.check_valid('$x cast as xs:integer', 'xs:integer')
        self.check_valid('$x castable as xs:integer', 'xs:boolean')
        self.check_valid('$x instance of xs:double', 'xs:boolean')
        self.check_valid('$x treat as xs:double', 'xs:double')

    def test_node_sequence_to_numeric_function(self):
        # Nodes are atomized, so this is accepted by the function
        # conversion rules (the result depends on runtime values).
        self.check_valid('fn:abs($nodes)', 'xs:numeric?')
        self.check_valid('fn:abs(//a)', 'xs:numeric?')

    def test_comma_operator(self):
        self.check_valid('(1, 2)', 'xs:integer+')
        self.check_valid("(1, 'a')", 'xs:anyAtomicType+')

    def test_parser_method(self):
        result = self.parser.check_static_types('fn:abs($x)')
        self.assertIsInstance(result, StaticAnalysisResult)
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'xs:numeric?')

    def test_xpath31_expressions(self):
        parser = XPath31Parser()
        for source, sequence_type in [
                ('let $x := 1 return $x + 1', 'xs:integer'),
                ("'a' || 'b'", 'xs:string'),
                ('//a ! string()', 'xs:string*')]:
            result = analyze_expression(source, parser)
            self.assertTrue(result.ok, f'unexpected errors for {source!r}')
            self.assertEqual(result.sequence_type, sequence_type)


class StaticAnalysisRuntimeDependentTest(unittest.TestCase):
    """Expressions whose validity depends on runtime values: no errors
    can be reported and the result type can be unknown."""

    @classmethod
    def setUpClass(cls):
        cls.parser = XPath2Parser(variable_types={'x': 'xs:double'})

    def test_undeclared_variable(self):
        result = analyze_expression('$u + 1', self.parser)
        self.assertTrue(result.ok)
        self.assertTrue(result.unknown)
        self.assertIsNone(result.sequence_type)

    def test_context_item_with_default_type(self):
        result = analyze_expression('fn:name(.)', self.parser)
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'xs:string')

    def test_node_sequence_cardinality(self):
        # fn:abs(//a) fails at runtime only if more than one node is selected
        result = analyze_expression('fn:abs(//a)', self.parser)
        self.assertTrue(result.ok)

    def test_external_function_without_signature(self):
        calls = []

        def my_func(x):
            calls.append(x)
            return x

        parser = XPath2Parser()
        parser.external_function(my_func, name='my-f')
        result = analyze_expression('my-f($x)', parser)
        self.assertTrue(result.ok)
        self.assertTrue(result.unknown)
        self.assertEqual(calls, [])

    def test_external_function_with_signature(self):
        calls = []

        def my_func(x):
            calls.append(x)
            return x

        parser = XPath2Parser()
        parser.external_function(my_func, name='my-g',
                                 sequence_types=('xs:integer', 'xs:integer'))

        result = analyze_expression('my-g($x)', parser)
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'xs:integer')

        result = analyze_expression('my-g($x, 1)', parser)
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])


class StaticAnalysisSideEffectsTest(unittest.TestCase):
    """The static analysis must not consume variable values, execute user
    functions or load external documents."""

    def test_analysis_does_not_consume_variables(self):
        TrackingList.iterated = False
        parser = XPath2Parser(variable_types={'it': 'xs:integer*'})

        result = parser.check_static_types('for $x in $it return $x * 2')
        self.assertTrue(result.ok)
        self.assertFalse(TrackingList.iterated)

        # The evaluation of the same expression still gets all the values
        values = TrackingList([1, 2, 3])
        root = ElementTree.XML('<root/>')
        context = XPathContext(root, variables={'it': values})
        root_token = parser.parse('for $x in $it return $x * 2')
        self.assertEqual(list(root_token.select(context)), [2, 4, 6])
        self.assertTrue(TrackingList.iterated)

    def test_analysis_does_not_execute_user_functions(self):
        calls = []

        def my_func(x):
            calls.append(x)
            return 0

        parser = XPath2Parser(variable_types={'x': 'xs:double'})
        parser.external_function(my_func, name='my-f',
                                 sequence_types=('xs:double', 'xs:integer'))
        result = parser.check_static_types('my-f($x)')
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'xs:integer')
        self.assertEqual(calls, [])

    def test_analysis_does_not_load_external_documents(self):
        parser = XPath2Parser()
        uri = 'file:///nonexistent-static-analysis-dir/doc.xml'

        result = parser.check_static_types(f"fn:doc('{uri}')")
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'document-node()?')

        # The evaluation of the same expression fails because the
        # document does not exist
        root = ElementTree.XML('<root/>')
        root_token = parser.parse(f"fn:doc('{uri}')")
        with self.assertRaises(ElementPathError) as ctx:
            root_token.evaluate(XPathContext(root))
        self.assertEqual(ctx.exception.code, 'err:FODC0002')

    def test_document_types_static_context(self):
        uri = 'file:///nonexistent-static-analysis-dir/doc.xml'
        parser = XPath2Parser(document_types={uri: 'document-node()'})
        result = parser.check_static_types(f"fn:doc('{uri}')")
        self.assertTrue(result.ok)
        self.assertEqual(result.sequence_type, 'document-node()')


if __name__ == '__main__':
    unittest.main()
