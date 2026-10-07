#!/usr/bin/env python
#
# Copyright (c), 2018-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import unittest
import io
import xml.etree.ElementTree as ElementTree

from elementpath import select, UnsupportedFeatureError
from elementpath.streaming import StreamingSelector, iter_select_stream


XML_DOCUMENT = '''<root>
  <item id="1"><name>a</name></item>
  <item id="2" kind="x"><name>b</name><extra/></item>
  <other>not an item</other>
  <item id="3"><name>c</name></item>
  <wrap><item id="4" kind="x"><name>d</name></item></wrap>
</root>'''


def make_events(source, counted=None):
    for event, elem in ElementTree.iterparse(io.StringIO(source),
                                             events=('start', 'end')):
        if counted is not None:
            counted[0] += 1
        yield event, elem


def signatures(elements):
    return [(e.tag, e.get('id'), (e.text or '').strip()) for e in elements]


class TestStreamingCompileTime(unittest.TestCase):
    """Compile-time validation of the supported XPath subset."""

    def test_supported_expressions(self):
        for path in ('item', 'root/item', '/root/item', '//item', 'root//item',
                     'item/name', 'item/*', '*', 'item[@id]', 'item[not(@id)]',
                     'item[@id="1"]', 'item[@id!="1"]', 'item[@n>2]',
                     'item[@id="1" and @kind="x"]', 'item[@id="1" or @kind="x"]',
                     'item[2]', 'item[position()=2]', 'item[position()<3]',
                     'item[position()>=2]', 'item[2][@id]', 'item[@id][2]',
                     'child::item', 'root/descendant::item'):
            try:
                StreamingSelector(path)
            except UnsupportedFeatureError as err:
                self.fail(f'{path!r} unexpectedly rejected: {err}')

    def test_supported_namespaced_expressions(self):
        namespaces = {'p': 'http://example.com/ns'}
        for path in ('p:item', 'p:*', 'p:item[@p:id="1"]', '//p:item/p:name'):
            try:
                StreamingSelector(path, namespaces=namespaces)
            except UnsupportedFeatureError as err:
                self.fail(f'{path!r} unexpectedly rejected: {err}')

    def test_rejected_expressions(self):
        for path in ('//item[last()]', '//item[position()=last()]',
                     '//item[position()=last()-1]',
                     '//item/following::x', '//item/preceding::x',
                     '//item/following-sibling::x', '//item/preceding-sibling::x',
                     '//item/ancestor::root', '//item/..', '//item/@id',
                     '//item[name]', '//item[.//name]', '//item/text()',
                     '//item/node()', 'item | other', '//item[1+1]',
                     '//item[starts-with(@id, "1")]'):
            with self.assertRaises(UnsupportedFeatureError, msg=path):
                StreamingSelector(path)

    def test_rejection_happens_at_compile_time(self):
        # The expression is validated when the selector is created,
        # before consuming any event.
        with self.assertRaises(UnsupportedFeatureError):
            iter_select_stream(iter([]), '//item[last()]')

        def events():
            raise AssertionError('event source must not be touched')
            yield

        with self.assertRaises(UnsupportedFeatureError):
            iter_select_stream(events(), '//item/following::x')


class TestStreamingSelect(unittest.TestCase):
    """Streaming selection compared with the standard select() function."""

    def assert_same_selection(self, path, namespaces=None):
        root = ElementTree.XML(XML_DOCUMENT)
        expected = signatures(select(root, path, namespaces=namespaces))
        result = signatures(iter_select_stream(
            make_events(XML_DOCUMENT), path, namespaces=namespaces))
        self.assertEqual(result, expected, msg=path)

    def test_forward_element_paths(self):
        for path in ('item', 'root/item', '/root/item', '//item',
                     'root//item', '//item/name', 'root/item/name',
                     '//wrap/item', '/*', '/root/*'):
            self.assert_same_selection(path)

    def test_attribute_predicates(self):
        for path in ('//item[@id]', '//item[@id="2"]', '//item[@id!="2"]',
                     '//item[not(@kind)]', '//item[@kind="x"]',
                     '//item[@id="2" and @kind="x"]',
                     '//item[@id="1" or @kind="x"]'):
            self.assert_same_selection(path)

    def test_position_predicates(self):
        for path in ('//item[1]', '//item[2]', '//item[3]',
                     '//item[position()=2]', '//item[position()<3]',
                     '//item[position()<=2]', '//item[position()>2]',
                     '//item[position()>=3]', '//item[position()!=2]',
                     'root/item[2]', '//item[@kind][1]', '//item[2][@id]'):
            self.assert_same_selection(path)

    def test_namespaces(self):
        source = ('<root xmlns="http://example.com/ns">'
                  '<item id="1"/><item id="2"/></root>')
        namespaces = {'': 'http://example.com/ns'}
        root = ElementTree.XML(source)
        expected = select(root, 'item', namespaces=namespaces)
        result = list(iter_select_stream(make_events(source), 'item',
                                         namespaces=namespaces))
        self.assertEqual([e.get('id') for e in result],
                         [e.get('id') for e in expected])

        source = ('<p:root xmlns:p="http://example.com/ns">'
                  '<p:item p:id="1"/><p:item p:id="2"/></p:root>')
        namespaces = {'p': 'http://example.com/ns'}
        root = ElementTree.XML(source)
        expected = select(root, 'p:item[@p:id="2"]', namespaces=namespaces)
        result = list(iter_select_stream(make_events(source), 'p:item[@p:id="2"]',
                                         namespaces=namespaces))
        self.assertEqual([e.get('{http://example.com/ns}id') for e in result],
                         [e.get('{http://example.com/ns}id') for e in expected])

    def test_selected_subtrees_are_delivered_complete(self):
        result = list(iter_select_stream(make_events(XML_DOCUMENT), '//item'))
        self.assertEqual(len(result), 4)
        for elem in result:
            names = [child.tag for child in elem]
            self.assertIn('name', names)
        item2 = result[1]
        self.assertEqual(item2.find('name').text, 'b')
        self.assertEqual(item2.find('extra').tag, 'extra')

    def test_first_match_returned_before_document_end(self):
        source = '<root>' + ''.join(
            f'<item id="{i}"><value/></item>' for i in range(1000)
        ) + '</root>'
        total = [0]
        list(iter_select_stream(make_events(source, total), '//item'))

        counted = [0]
        selector = iter_select_stream(make_events(source, counted), '//item[@id="2"]')
        first = next(selector)
        self.assertEqual(first.get('id'), '2')
        self.assertLess(counted[0], total[0])
        self.assertEqual(len(first), 1)  # subtree is complete
        selector.close()

    def test_nested_matches(self):
        source = '<root><a id="1"><a id="2"><a id="3"/></a></a></root>'
        result = list(iter_select_stream(make_events(source), '//a'))
        # Inner matches complete first; enclosing matches are delivered
        # afterwards with their subtree still intact.
        self.assertEqual([e.get('id') for e in result], ['3', '2', '1'])
        outer = result[-1]
        self.assertEqual(outer.get('id'), '1')
        self.assertEqual([c.get('id') for c in outer], ['2'])
        self.assertEqual([c.get('id') for c in outer[0]], ['3'])

    def test_consumer_early_exit_stops_event_consumption(self):
        source = '<root>' + ''.join(
            f'<item id="{i}"/>' for i in range(1000)
        ) + '</root>'
        counted = [0]
        selector = iter_select_stream(make_events(source, counted), '//item')
        first = next(selector)
        self.assertEqual(first.get('id'), '0')
        consumed = counted[0]
        selector.close()
        self.assertEqual(counted[0], consumed)
        self.assertLess(counted[0], 100)

    def test_completed_nodes_are_released(self):
        holder = {}

        def events():
            for event, elem in ElementTree.iterparse(
                    io.StringIO(XML_DOCUMENT), events=('start', 'end')):
                holder.setdefault('root', elem)
                yield event, elem

        result = list(iter_select_stream(events(), '//item/name'))
        self.assertEqual([e.text for e in result], ['a', 'b', 'c', 'd'])
        # Completed nodes that are not selected are cleared and detached.
        root = holder['root']
        self.assertEqual(len(root), 0)
        self.assertIsNone(root.text)

    def test_ancestor_state_preserved_for_active_matches(self):
        # While a match is open its ancestors are not cleared, and matched
        # elements nested inside another match keep their subtree.
        source = ('<root><section id="s1"><p>text</p>'
                  '<section id="s2"><p>inner</p></section></section></root>')
        result = list(iter_select_stream(make_events(source), '//section'))
        self.assertEqual([e.get('id') for e in result], ['s2', 's1'])
        inner, outer = result
        self.assertEqual(inner.find('p').text, 'inner')
        self.assertEqual(outer.find('p').text, 'text')
        self.assertEqual(outer.find('section').get('id'), 's2')

    def test_ignores_other_events(self):
        source = '<root><item id="1"/></root>'

        def events():
            yield 'start-ns', ('', 'http://example.com/ns')
            for event, elem in ElementTree.iterparse(
                    io.StringIO(source), events=('start', 'end')):
                yield event, elem
            yield 'end-ns', None

        result = list(iter_select_stream(events(), '//item'))
        self.assertEqual([e.get('id') for e in result], ['1'])


if __name__ == '__main__':
    unittest.main()
