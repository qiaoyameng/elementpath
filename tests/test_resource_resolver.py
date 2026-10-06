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

from elementpath import XPath2Parser, XPathContext, ResourceResolver
from elementpath.exceptions import ElementPathValueError, ElementPathTypeError
from elementpath.resource_resolver import normalize_uri, sanitize_uri
from elementpath.xpath_nodes import DocumentNode


class TestUriHelpers(unittest.TestCase):

    def test_normalize_uri(self):
        self.assertEqual(normalize_uri('a.xml'), 'a.xml')
        self.assertEqual(
            normalize_uri('a.xml', 'https://example.test/docs/'),
            'https://example.test/docs/a.xml'
        )
        self.assertEqual(
            normalize_uri('./sub/../a.xml', 'https://example.test/docs/'),
            'https://example.test/docs/a.xml'
        )
        self.assertEqual(
            normalize_uri('HTTPS://example.test/docs/./a.xml'),
            'https://example.test/docs/a.xml'
        )
        self.assertEqual(
            normalize_uri('  https://example.test/a.xml  '),
            'https://example.test/a.xml'
        )

    def test_sanitize_uri(self):
        self.assertEqual(
            sanitize_uri('https://user:secret@example.test/docs/a.xml'),
            'https://example.test/docs/a.xml'
        )
        self.assertEqual(
            sanitize_uri('https://user:secret@example.test:8080/a.xml'),
            'https://example.test:8080/a.xml'
        )
        self.assertEqual(
            sanitize_uri('https://example.test/docs/a.xml'),
            'https://example.test/docs/a.xml'
        )


class TestResourceResolver(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser = XPath2Parser(base_uri='https://example.test/docs/')
        cls.sources = {
            'https://example.test/docs/a.xml': '<a><b1/></a>',
            'https://example.test/docs/b.xml': '<b><b2/></b>',
            'https://example.test/docs/c.xml': '<c><b3/></c>',
        }
        cls.loads = []

    @classmethod
    def in_memory_loader(cls, uri):
        cls.loads.append(uri)
        try:
            return ElementTree.fromstring(cls.sources[uri])
        except KeyError:
            return None

    def make_context(self, resolver):
        root = ElementTree.XML('<root/>')
        return XPathContext(root, resources=resolver)

    def evaluate(self, expression, context):
        return self.parser.parse(expression).evaluate(context)

    def test_register_document(self):
        document = ElementTree.fromstring('<a><b1/></a>')
        resolver = ResourceResolver()
        resolver.register_document('https://example.test/docs/a.xml', document)

        context = self.make_context(resolver)
        result = self.evaluate("fn:doc('a.xml')", context)
        self.assertIsInstance(result, DocumentNode)
        self.assertEqual(result.getroot().name, 'a')
        self.assertTrue(self.evaluate("fn:doc-available('a.xml')", context))

    def test_unknown_uri_is_rejected(self):
        resolver = ResourceResolver(loaders=[self.in_memory_loader])
        context = self.make_context(resolver)

        with self.assertRaises(ElementPathValueError) as err:
            self.evaluate("fn:doc('missing.xml')", context)
        self.assertIn('FODC0002', str(err.exception))
        self.assertIn('https://example.test/docs/missing.xml', str(err.exception))

        self.assertFalse(self.evaluate("fn:doc-available('missing.xml')", context))

        with self.assertRaises(ElementPathValueError) as err:
            self.evaluate("fn:collection('missing')", context)
        self.assertIn('FODC0002', str(err.exception))

        with self.assertRaises(ElementPathValueError) as err:
            self.evaluate("fn:collection()", context)
        self.assertIn('FODC0002', str(err.exception))

    def test_document_node_identity(self):
        resolver = ResourceResolver(loaders=[self.in_memory_loader])
        context = self.make_context(resolver)

        doc1 = self.evaluate("fn:doc('a.xml')", context)
        doc2 = self.evaluate("fn:doc('./a.xml')", context)
        doc3 = self.evaluate("fn:doc('sub/../a.xml')", context)
        self.assertIs(doc1, doc2)
        self.assertIs(doc1, doc3)

    def test_doc_and_collection_share_node_identity(self):
        resolver = ResourceResolver(
            collections={'https://example.test/docs/agg': ['a.xml', 'b.xml']},
            loaders=[self.in_memory_loader],
        )
        context = self.make_context(resolver)

        document = self.evaluate("fn:doc('a.xml')", context)
        collection = self.evaluate("fn:collection('agg')", context)
        self.assertEqual(len(collection), 2)
        self.assertIs(collection[0], document)

    def test_collection_stable_order_and_duplicates(self):
        type(self).loads = []
        resolver = ResourceResolver(
            collections={'https://example.test/docs/agg':
                         ['b.xml', 'a.xml', 'c.xml', 'a.xml']},
            loaders=[self.in_memory_loader],
        )
        context = self.make_context(resolver)

        collection = self.evaluate("fn:collection('agg')", context)
        self.assertEqual([x.getroot().name for x in collection],
                         ['b', 'a', 'c', 'a'])
        # Repeated URIs share the same node, no copies are created
        self.assertIs(collection[1], collection[3])
        self.assertEqual(self.loads.count('https://example.test/docs/a.xml'), 1)

    def test_default_collection(self):
        resolver = ResourceResolver(
            default_collection=['a.xml', 'b.xml'],
            loaders=[self.in_memory_loader],
        )
        context = self.make_context(resolver)

        collection = self.evaluate("fn:collection()", context)
        self.assertEqual([x.getroot().name for x in collection], ['a', 'b'])

    def test_loader_error_hides_credentials(self):
        def failing_loader(uri):
            raise RuntimeError(f"connection to {uri} refused")

        resolver = ResourceResolver(loaders=[failing_loader])
        context = self.make_context(resolver)

        with self.assertRaises(ElementPathValueError) as err:
            self.evaluate(
                "fn:doc('https://user:secret@example.test/docs/a.xml')", context
            )
        message = str(err.exception)
        self.assertIn('FODC0002', message)
        self.assertIn('https://example.test/docs/a.xml', message)
        self.assertNotIn('secret', message)
        self.assertNotIn('user', message)

    def test_parallel_resolvers_have_independent_caches(self):
        sources_a = {'https://example.test/docs/a.xml': '<a>one</a>'}
        sources_b = {'https://example.test/docs/a.xml': '<a>two</a>'}

        resolver_a = ResourceResolver(
            loaders=[lambda uri: ElementTree.fromstring(sources_a[uri])
                     if uri in sources_a else None]
        )
        resolver_b = ResourceResolver(
            loaders=[lambda uri: ElementTree.fromstring(sources_b[uri])
                     if uri in sources_b else None]
        )
        context_a = self.make_context(resolver_a)
        context_b = self.make_context(resolver_b)

        # Interleave evaluations of the same URI with the two contexts
        doc_a1 = self.evaluate("fn:doc('a.xml')", context_a)
        doc_b1 = self.evaluate("fn:doc('a.xml')", context_b)
        doc_a2 = self.evaluate("fn:doc('a.xml')", context_a)
        doc_b2 = self.evaluate("fn:doc('a.xml')", context_b)

        self.assertIs(doc_a1, doc_a2)
        self.assertIs(doc_b1, doc_b2)
        self.assertIsNot(doc_a1, doc_b1)
        self.assertEqual(doc_a1.getroot().value.text, 'one')
        self.assertEqual(doc_b1.getroot().value.text, 'two')

    def test_register_loader(self):
        resolver = ResourceResolver()
        with self.assertRaises(ElementPathTypeError):
            resolver.register_loader('not-a-callable')

        loader = self.in_memory_loader
        resolver.register_loader(loader)
        resolver.register_loader(loader)
        self.assertEqual(len(resolver._loaders), 1)

    def test_clear_cache(self):
        resolver = ResourceResolver(loaders=[self.in_memory_loader])
        context = self.make_context(resolver)

        doc1 = self.evaluate("fn:doc('a.xml')", context)
        resolver.clear_cache()
        doc2 = self.evaluate("fn:doc('a.xml')", context)
        self.assertIsNot(doc1, doc2)

    def test_resolver_takes_precedence_over_documents(self):
        resolver = ResourceResolver(loaders=[self.in_memory_loader])
        root = ElementTree.XML('<root/>')
        other = ElementTree.fromstring('<other/>')
        context = XPathContext(
            root, resources=resolver,
            documents={'https://example.test/docs/a.xml': other},
        )
        result = self.evaluate("fn:doc('a.xml')", context)
        self.assertEqual(result.getroot().name, 'a')


if __name__ == '__main__':
    unittest.main()
