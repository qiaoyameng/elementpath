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
import threading
import unittest
from xml.etree import ElementTree

from elementpath import XPath2Parser, XPathContext, ResourceContext, ResourceError
from elementpath.resources import normalize_uri, safe_uri


def make_loader(data):
    """Builds an in-memory loader from a mapping of absolute URIs to XML strings."""
    def loader(uri):
        return ElementTree.parse(io.StringIO(data[uri]))
    return loader


class TestUriHelpers(unittest.TestCase):

    def test_normalize_uri_resolves_relative_references(self):
        self.assertEqual(
            normalize_uri('a.xml', 'http://example.com/base/'),
            'http://example.com/base/a.xml'
        )
        self.assertEqual(
            normalize_uri('./sub/../a.xml', 'http://example.com/base/'),
            'http://example.com/base/a.xml'
        )
        self.assertEqual(
            normalize_uri('HTTP://example.com/base/a.xml'),
            'http://example.com/base/a.xml'
        )
        self.assertEqual(normalize_uri('a.xml'), 'a.xml')

    def test_safe_uri_strips_credentials(self):
        self.assertEqual(
            safe_uri('http://user:secret@example.com/x.xml'),
            'http://example.com/x.xml'
        )
        self.assertEqual(
            safe_uri('http://user:secret@example.com:8080/x.xml'),
            'http://example.com:8080/x.xml'
        )
        self.assertEqual(safe_uri('http://example.com/x.xml'),
                         'http://example.com/x.xml')
        self.assertEqual(safe_uri('a.xml'), 'a.xml')


class TestResourceContext(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.parser = XPath2Parser()
        cls.parser.base_uri = 'http://example.com/base/'
        cls.root = ElementTree.XML('<root/>')

    def make_context(self, data, collections=None, default_collection=None):
        loader = make_loader(data)
        resources = ResourceContext(base_uri='http://example.com/base/',
                                    loader=loader)
        for uri in data:
            resources.add_document(uri)
        for uri, members in (collections or {}).items():
            resources.add_collection(uri, members)
        if default_collection is not None:
            resources.set_default_collection(default_collection)
        return XPathContext(self.root, resources=resources)

    def evaluate(self, expression, context):
        return self.parser.parse(expression).evaluate(context)

    def test_no_default_loader(self):
        resources = ResourceContext()
        self.assertIsNone(resources.loader)

        resources.add_document('http://example.com/a.xml')
        with self.assertRaises(ResourceError) as err:
            resources.get_document('http://example.com/a.xml')
        self.assertIn('no loader available', str(err.exception))

    def test_relative_uri_resolution_and_identity(self):
        data = {'http://example.com/base/a.xml': '<a/>'}
        context = self.make_context(data)

        self.assertTrue(self.evaluate("doc('a.xml') is doc('a.xml')", context))
        self.assertTrue(self.evaluate("doc('./a.xml') is doc('a.xml')", context))
        self.assertTrue(self.evaluate("doc('sub/../a.xml') is doc('a.xml')", context))
        self.assertTrue(self.evaluate(
            "doc('http://example.com/base/a.xml') is doc('a.xml')", context
        ))

    def test_doc_collection_node_identity(self):
        data = {
            'http://example.com/base/a.xml': '<a><x/></a>',
            'http://example.com/base/b.xml': '<b/>',
        }
        context = self.make_context(data, collections={
            'coll': ['a.xml', 'b.xml'],
        })

        self.assertTrue(self.evaluate(
            "doc('a.xml') is collection('coll')[1]", context
        ))
        self.assertTrue(self.evaluate(
            "doc('b.xml') is collection('coll')[2]", context
        ))

    def test_collection_stable_order_and_no_duplicates(self):
        data = {
            'http://example.com/base/a.xml': '<a/>',
            'http://example.com/base/b.xml': '<b/>',
        }
        context = self.make_context(data, collections={
            'coll': ['a.xml', 'b.xml', 'a.xml'],
        })

        self.assertEqual(self.evaluate("count(collection('coll'))", context), 2)
        self.assertEqual(
            self.evaluate("collection('coll')/*/name()", context), ['a', 'b']
        )

    def test_default_collection(self):
        data = {'http://example.com/base/a.xml': '<a/>'}
        context = self.make_context(data, default_collection=['a.xml'])

        self.assertTrue(self.evaluate("collection() is doc('a.xml')", context))

        resources = ResourceContext()
        context = XPathContext(self.root, resources=resources)
        with self.assertRaises(Exception) as err:
            self.evaluate("collection()", context)
        self.assertIn('FODC0002', str(err.exception))

    def test_unknown_uri_rejected(self):
        context = self.make_context({})

        with self.assertRaises(Exception) as err:
            self.evaluate("doc('missing.xml')", context)
        self.assertIn('FODC0002', str(err.exception))
        self.assertIn('http://example.com/base/missing.xml', str(err.exception))

        self.assertFalse(self.evaluate("doc-available('missing.xml')", context))

        with self.assertRaises(Exception) as err:
            self.evaluate("collection('missing')", context)
        self.assertIn('FODC0002', str(err.exception))

    def test_loader_error_keeps_identifier_hides_credentials(self):
        def loader(uri):
            raise OSError('permission denied for user:secret')

        resources = ResourceContext(loader=loader)
        resources.add_document('http://user:secret@example.com/x.xml')

        with self.assertRaises(ResourceError) as err:
            resources.get_document('http://user:secret@example.com/x.xml')
        message = str(err.exception)
        self.assertIn('http://example.com/x.xml', message)
        self.assertNotIn('secret', message)
        self.assertIsInstance(err.exception.__cause__, OSError)

        context = XPathContext(self.root, resources=resources)
        with self.assertRaises(Exception) as xp_err:
            self.evaluate("doc('http://user:secret@example.com/x.xml')", context)
        self.assertIn('FODC0002', str(xp_err.exception))
        self.assertNotIn('secret', str(xp_err.exception))

    def test_registered_content_without_loader(self):
        document = ElementTree.parse(io.StringIO('<a/>'))
        resources = ResourceContext(base_uri='http://example.com/base/')
        resources.add_document('a.xml', document)

        context = XPathContext(self.root, resources=resources)
        self.assertTrue(self.evaluate("doc('a.xml') is doc('a.xml')", context))

    def test_parallel_contexts_do_not_share_identity_cache(self):
        data_a = {'http://example.com/shared.xml': '<a/>'}
        data_b = {'http://example.com/shared.xml': '<b/>'}

        resources_a = ResourceContext(loader=make_loader(data_a))
        resources_b = ResourceContext(loader=make_loader(data_b))
        resources_a.add_document('http://example.com/shared.xml')
        resources_b.add_document('http://example.com/shared.xml')

        barrier = threading.Barrier(4)
        results = {'a': [], 'b': []}
        errors = []

        def worker(resources, key):
            context = XPathContext(self.root, resources=resources)
            token = self.parser.parse("doc('http://example.com/shared.xml')")
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    results[key].append(token.evaluate(context))
            except Exception as err:  # pragma: no cover
                errors.append(err)

        threads = [
            threading.Thread(target=worker, args=(resources_a, 'a'))
            for _ in range(2)
        ] + [
            threading.Thread(target=worker, args=(resources_b, 'b'))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(errors)
        self.assertEqual(len(results['a']), 40)
        self.assertEqual(len(results['b']), 40)

        # Identity is stable within each resource context ...
        self.assertTrue(all(node is results['a'][0] for node in results['a']))
        self.assertTrue(all(node is results['b'][0] for node in results['b']))

        # ... but the two contexts do not share the identity cache.
        self.assertIsNot(results['a'][0], results['b'][0])
        self.assertEqual(results['a'][0].getroot().value.tag, 'a')
        self.assertEqual(results['b'][0].getroot().value.tag, 'b')


if __name__ == '__main__':
    unittest.main()
