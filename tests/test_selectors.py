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
import io
import unittest
import warnings
import xml.etree.ElementTree as ElementTree

from elementpath import select, iter_select, iter_select_events, iterparse_select, \
    Selector, XPath2Parser, UnsupportedFeatureError

try:
    import lxml.etree as lxml_etree
except ImportError:
    lxml_etree = None


class XPathSelectorsTest(unittest.TestCase):
    etree = ElementTree

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = cls.etree.XML('<author>Dickens</author>')

    def test_select_function(self):
        self.assertEqual(select(self.root, 'text()'), ['Dickens'])
        self.assertEqual(select(self.root, '$a', variables={'a': 1}), 1)

        self.assertEqual(
            select(self.root, '$a', variables={'a': 1}, variable_types={'a': 'xs:decimal'}), 1
        )

    def test_iter_select_function(self):
        self.assertEqual(list(iter_select(self.root, 'text()')), ['Dickens'])
        self.assertEqual(list(iter_select(self.root, '$a', variables={'a': True})), [True])

    def test_selector_class(self):
        selector = Selector('/A')
        self.assertEqual(repr(selector), "Selector(path='/A', parser=XPath2Parser)")
        self.assertEqual(selector.namespaces, XPath2Parser.DEFAULT_NAMESPACES)

        selector = Selector('text()')
        self.assertEqual(selector.select(self.root), ['Dickens'])
        self.assertEqual(list(selector.iter_select(self.root)), ['Dickens'])

        selector = Selector('$a')
        self.assertEqual(selector.select(self.root, variables={'a': 1}), 1)
        self.assertEqual(list(selector.iter_select(self.root, variables={'a': 1})), [1])

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter('always')
            selector = Selector('$a', variables={'a': 1})
            self.assertEqual(len(w), 1)
            self.assertEqual(w[0].category, DeprecationWarning)

        self.assertEqual(selector.select(self.root), 1)
        self.assertEqual(list(selector.iter_select(self.root)), [1])

    def test_issue_001(self):
        selector = Selector("//FullPath[ends-with(., 'Temp')]")
        self.assertEqual(selector.select(self.etree.XML('<A/>')), [])
        self.assertEqual(selector.select(self.etree.XML('<FullPath/>')), [])
        root = self.etree.XML('<FullPath>High Temp</FullPath>')
        self.assertEqual(selector.select(root), [root])

    def test_issue_042(self):
        selector1 = Selector('text()')
        selector2 = Selector('sup[last()]/preceding-sibling::text()')
        root = self.etree.XML('<root>a<sup>1</sup>b<sup>2</sup>c<sup>3</sup></root>')
        self.assertEqual(selector1.select(root), selector2.select(root))

        selector2 = Selector('sup[1]/following-sibling::text()')
        root = self.etree.XML('<root><sup>1</sup>b<sup>2</sup>c<sup>3</sup>d</root>')
        self.assertEqual(selector1.select(root), selector2.select(root))

    def test_fragment_argument__issue_081(self):
        # xml1 contains the xml-stylesheet tag
        xml1 = b"""<?xml version="1.0" encoding="UTF-8"?>
        <?xml-stylesheet type='text/xsl' href='test.xsl'?>
        <root>
            <first>
                <second>
                    value
                </second>
            </first>
        </root>
        """

        # the same as xml1, but without the xml-stylesheet tag
        xml2 = b"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
        <root>
            <first>
                <second>
                    value
                </second>
            </first>
        </root>
        """

        root1 = self.etree.XML(xml1)
        root2 = self.etree.XML(xml2)
        query = "first/second"

        if hasattr(root1, 'xpath'):
            self.assertEqual(select(root1, query), [])
        else:
            self.assertEqual(select(root1, query), root1[0][:])

        self.assertEqual(select(root1, query, fragment=True), root1[0][:])
        self.assertEqual(select(root2, query), root2[0][:])

        self.assertEqual(select(root1, query, fragment=False), [])
        self.assertEqual(select(root2, query, fragment=False), [])

        query = "root/first/second"

        self.assertEqual(select(root1, query, fragment=False), root1[0][:])
        self.assertEqual(select(root2, query, fragment=False), root2[0][:])


class StreamEvents:
    def __init__(self, events):
        self.events = iter(events)
        self.pulled = 0
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        event = next(self.events)
        self.pulled += 1
        return event

    def close(self):
        self.closed = True


class StreamingSelectorTest(unittest.TestCase):
    XML = (
        b'<root>'
        b'<item id="1" kind="a"><nested id="1.1"/></item>'
        b'<garbage><deep/></garbage>'
        b'<item id="2" kind="b"/>'
        b'<item id="3" kind="a"/>'
        b'</root>'
    )

    def events(self, xml=None):
        return ElementTree.iterparse(
            io.BytesIO(xml or self.XML), events=('start', 'end')
        )

    def test_matches_document_select(self):
        document = ElementTree.parse(io.BytesIO(self.XML))
        paths = [
            '/root/item',
            '//item',
            '/root/item[@kind="a"]',
            '/root/item[@kind="a"][2]',
            '//nested',
            '/root/item[2]',
        ]
        for path in paths:
            with self.subTest(path=path):
                results = list(iter_select_events(self.events(), path))
                expected = select(document, path)
                self.assertEqual(
                    [(element.tag, element.attrib) for element in results],
                    [(element.tag, element.attrib) for element in expected],
                )

    def test_first_match_returns_before_document_ends(self):
        root = ElementTree.Element('root')
        item = ElementTree.SubElement(root, 'item', {'id': '1'})
        nested = ElementTree.SubElement(item, 'nested')
        tail = ElementTree.SubElement(root, 'tail')

        def build_events():
            yield ('start', root)
            yield ('start', item)
            yield ('start', nested)
            yield ('end', nested)
            yield ('end', item)
            yield ('start', tail)
            yield ('end', tail)
            yield ('end', root)

        events = StreamEvents(build_events())
        selector = iter_select_events(events, '/root/item')
        match = next(selector)

        self.assertIs(match, item)
        self.assertEqual([child.tag for child in match], ['nested'])
        self.assertEqual(events.pulled, 5)
        selector.close()
        self.assertTrue(events.closed)

    def test_nested_matches_remain_complete_and_document_ordered(self):
        xml = (
            b'<root><item id="outer">'
            b'<item id="middle"><item id="inner"/></item>'
            b'</item></root>'
        )
        results = list(iter_select_events(self.events(xml), '//item'))

        self.assertEqual([item.get('id') for item in results],
                         ['outer', 'middle', 'inner'])
        self.assertEqual(results[0][0].get('id'), 'middle')
        self.assertEqual(results[0][0][0].get('id'), 'inner')
        self.assertIs(results[1][0], results[2])

    def test_completed_nonmatches_are_released_but_active_ancestor_is_kept(self):
        root = ElementTree.Element('root')

        def events():
            yield ('start', root)
            garbage = ElementTree.SubElement(root, 'garbage')
            yield ('start', garbage)
            deep = ElementTree.SubElement(garbage, 'deep')
            yield ('start', deep)
            yield ('end', deep)
            yield ('end', garbage)
            target = ElementTree.SubElement(root, 'target')
            yield ('start', target)
            payload = ElementTree.SubElement(target, 'payload')
            yield ('start', payload)
            yield ('end', payload)
            yield ('end', target)
            tail = ElementTree.SubElement(root, 'tail')
            yield ('start', tail)
            yield ('end', tail)
            yield ('end', root)

        selector = iter_select_events(events(), '/root/target')
        match = next(selector)

        self.assertEqual(match.tag, 'target')
        self.assertEqual(len(root), 1)
        self.assertIs(root[0], match)
        self.assertEqual([child.tag for child in match], ['payload'])
        selector.close()

    def test_namespace_and_numeric_attribute_predicates(self):
        xml = b'<root xmlns="urn:test"><item n="2"/><item n="10"/><item n="x"/></root>'
        events = ElementTree.iterparse(io.BytesIO(xml), events=('start', 'end'))
        results = list(iter_select_events(
            events, '/t:root/t:item[@n > 1]',
            namespaces={'t': 'urn:test'},
        ))
        self.assertEqual([item.attrib['n'] for item in results], ['2', '10'])

        wildcard_events = ElementTree.iterparse(
            io.BytesIO(b'<r xmlns="urn:test"><a/><b/></r>'),
            events=('start', 'end'),
        )
        wildcard_results = list(iter_select_events(
            wildcard_events, '//*:a', namespaces={'t': 'urn:test'}
        ))
        self.assertEqual([item.tag for item in wildcard_results], ['{urn:test}a'])

    def test_unsupported_expressions_are_rejected_without_reading_events(self):
        class UnreadEvents:
            def __iter__(self):
                raise AssertionError('events must not be consumed')

        for path in ('a/following-sibling::b', 'preceding::b',
                     'a[last()]', 'a/@id', 'a | b', '.'):
            with self.subTest(path=path):
                with self.assertRaises(UnsupportedFeatureError):
                    iter_select_events(UnreadEvents(), path)

    def test_iterparse_select_stops_on_consumer_exit(self):
        stream = io.BytesIO(b'<root><target><x/></target><tail/></root>')
        selector = iterparse_select(stream, '/root/target')
        match = next(selector)

        self.assertEqual(match.tag, 'target')
        selector.close()

    def test_event_source_is_not_pulled_after_consumer_exit(self):
        root = ElementTree.Element('root')
        target = ElementTree.SubElement(root, 'target')
        nested = ElementTree.SubElement(target, 'nested')
        tail = ElementTree.SubElement(root, 'tail')

        def build_events():
            yield ('start', root)
            yield ('start', target)
            yield ('start', nested)
            yield ('end', nested)
            yield ('end', target)
            yield ('start', tail)
            yield ('end', tail)
            yield ('end', root)

        events = StreamEvents(build_events())
        selector = iter_select_events(events, '/root/target')
        self.assertIs(next(selector), target)
        self.assertEqual(events.pulled, 5)
        selector.close()
        self.assertTrue(events.closed)
        self.assertEqual(events.pulled, 5)


@unittest.skipIf(lxml_etree is None, "The lxml library is not installed")
class LxmlXPathSelectorsTest(XPathSelectorsTest):
    etree = lxml_etree

    def test_issue_058(self):
        tei = """<?xml version='1.0' encoding='UTF8'?>
        <?xml-model type="application/xml"?>
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <text>
                <pb n="page1"/>
                <pb n="page2"/>
          </text>
        </TEI>
        """

        doc = self.etree.XML(tei.encode())
        namespaces = {'': "http://www.tei-c.org/ns/1.0"}
        k = None
        for k, p in enumerate(select(doc, '//pb', namespaces), start=1):
            self.assertEqual(p.attrib['n'], f'page{k}')
            self.assertEqual(p.xpath('./@n'), [f'page{k}'])
            self.assertEqual(select(doc, './@n'), [])
            self.assertEqual(select(p, './@n'), [f'page{k}'])
            self.assertEqual(select(doc, './@n', item=p), [f'page{k}'])
        else:
            self.assertEqual(k, 2)

    def test_issue_074(self):
        root = lxml_etree.XML("<root><trunk><branch></branch></trunk></root>")

        result = select(root, "trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        result = select(root, "/root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        root = lxml_etree.XML("<!--comment--><root><trunk><branch></branch></trunk></root>")

        result = select(root, "trunk")
        self.assertEqual(result, [])

        result = select(root, "root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]

        result = select(root, "/root/trunk")
        self.assertEqual(result, [root[0]])  # [<Element trunk at 0x...>]


if __name__ == '__main__':
    unittest.main()
