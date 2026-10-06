#
# Copyright (c), 2018-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Explicit resource resolution for XPath resource functions.

This module provides the :class:`ResourceContext` class, an explicit registry
of the external resources that XPath expressions are allowed to access through
functions like fn:doc() and fn:collection(). Resources are not fetched from the
filesystem or the network by default: the caller registers the allowed URIs,
the collection members and an optional loader callable that is the only
component entitled to load resource content.
"""
import threading
import urllib.parse
from typing import Any, Optional

import elementpath.aliases as ta

from elementpath.exceptions import ElementPathError
from elementpath.xpath_nodes import XPathNode, DocumentNode
from elementpath.tree_builders import get_node_tree

__all__ = ['ResourceContext', 'ResourceError', 'normalize_uri', 'safe_uri']


def _remove_dot_segments(path: str) -> str:
    """Removes dot segments from a URI path, as defined by RFC 3986 section 5.2.4."""
    input_buffer = path
    output = ''
    while input_buffer:
        if input_buffer.startswith('../'):
            input_buffer = input_buffer[3:]
        elif input_buffer.startswith('./'):
            input_buffer = input_buffer[2:]
        elif input_buffer.startswith('/./'):
            input_buffer = '/' + input_buffer[3:]
        elif input_buffer == '/.':
            input_buffer = '/'
        elif input_buffer.startswith('/../'):
            input_buffer = '/' + input_buffer[4:]
            output = output[:output.rfind('/')] if '/' in output else ''
        elif input_buffer == '/..':
            input_buffer = '/'
            output = output[:output.rfind('/')] if '/' in output else ''
        elif input_buffer in ('.', '..'):
            input_buffer = ''
        else:
            if input_buffer.startswith('/'):
                index = input_buffer.find('/', 1)
            else:
                index = input_buffer.find('/')
            if index < 0:
                index = len(input_buffer)
            output += input_buffer[:index]
            input_buffer = input_buffer[index:]

    return output


def normalize_uri(uri: str, base_uri: Optional[str] = None) -> str:
    """
    Normalizes a URI reference, resolving it against an optional base URI.

    :param uri: a URI reference, can be relative.
    :param base_uri: an optional base URI used to resolve relative references.
    :returns: the normalized URI, with dot segments removed and the scheme \
    normalized to lowercase.
    """
    uri = uri.strip()
    if base_uri:
        uri = urllib.parse.urljoin(base_uri, uri)

    parts = urllib.parse.urlsplit(uri)
    path = _remove_dot_segments(parts.path)
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc, path, parts.query, parts.fragment)
    )


def safe_uri(uri: str) -> str:
    """
    Returns a URI string stripped of any credential information (the userinfo
    part of the authority), suitable for inclusion in error messages.
    """
    try:
        parts = urllib.parse.urlsplit(uri)
        if parts.username is None and parts.password is None:
            return uri
        host = parts.hostname or ''
        if parts.port is not None:
            host = f'{host}:{parts.port}'
    except ValueError:
        return uri

    return urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, parts.query, parts.fragment)
    )


class ResourceError(ElementPathError):
    """
    Raised when a resource cannot be resolved or loaded by a ResourceContext.
    The message includes the resource identifier with credentials stripped out.

    :param message: the error message.
    :param uri: the URI of the resource, sanitized before being included \
    in the error message.
    """
    def __init__(self, message: str, uri: Optional[str] = None) -> None:
        self.uri = uri
        if uri is not None:
            message = f'{message}: {safe_uri(uri)}'
        super().__init__(message)


class ResourceContext:
    """
    An explicit resource resolution context for XPath resource functions
    (fn:doc, fn:doc-available and fn:collection).

    Only the URIs registered by the caller can be resolved: unknown URIs are
    rejected and no implicit access to the filesystem or to the network is
    performed. Resource content is either registered directly or loaded on
    demand through the caller-provided *loader* callable. No default loader
    is provided.

    Document nodes obtained from the same absolute URI are cached per
    instance, so the same URI always maps to the same document node identity
    within the context. Distinct ResourceContext instances do not share any
    cached node. The instance is safe to use concurrently from multiple
    threads.

    :param base_uri: an optional base URI used to resolve relative URI \
    references of registered resources and collection members.
    :param loader: an optional callable that accepts an absolute URI string \
    and returns the resource content (an ElementTree document or element, \
    or an XPath node). It's the only component allowed to load resources \
    and it's never provided by default.
    :param namespaces: an optional mapping from prefixes to namespace URIs, \
    used when building node trees from loaded resources.
    """
    def __init__(self,
                 base_uri: Optional[str] = None,
                 loader: Optional[ta.LoaderType] = None,
                 namespaces: Optional[ta.NamespacesType] = None) -> None:
        self.base_uri = base_uri
        self.loader = loader
        self.namespaces = namespaces
        self._registry: dict[str, Optional[ta.RootArgType]] = {}
        self._documents: dict[str, DocumentNode] = {}
        self._collections: dict[str, list[Any]] = {}
        self._collection_nodes: dict[str, list[XPathNode]] = {}
        self._default_collection: Optional[list[Any]] = None
        self._default_collection_nodes: Optional[list[XPathNode]] = None
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return '%s(documents=%d, collections=%d)' % (
            self.__class__.__name__, len(self._registry), len(self._collections)
        )

    def resolve_uri(self, uri: str, base_uri: Optional[str] = None) -> str:
        """
        Resolves a URI reference against the static base URI and normalizes it.

        :param uri: a URI reference, can be relative.
        :param base_uri: an optional explicit base URI, otherwise the base \
        URI of the resource context is used.
        """
        return normalize_uri(uri, base_uri or self.base_uri)

    def add_document(self, uri: str, document: Optional[ta.RootArgType] = None) -> None:
        """
        Registers a document URI, optionally with its content. If no content
        is provided the registered loader is used when the document is first
        requested. Registering the same URI again replaces the previous
        registration and invalidates the cached node.

        :param uri: the URI of the document, resolved against the base URI \
        of the context if relative.
        :param document: the document content (an ElementTree document or \
        element, or an XPath node) or `None` for deferred loading.
        """
        uri = self.resolve_uri(uri)
        with self._lock:
            self._registry[uri] = document
            self._documents.pop(uri, None)

    def add_collection(self, uri: str, members: Any) -> None:
        """
        Registers a collection URI with its members. Members can be URI
        strings (resolved through the context, sharing the document node
        identity cache) or directly provided nodes or XML trees. The
        registration order is preserved.

        :param uri: the URI of the collection, resolved against the base \
        URI of the context if relative.
        :param members: an iterable of collection members.
        """
        uri = self.resolve_uri(uri)
        with self._lock:
            self._collections[uri] = list(members)
            self._collection_nodes.pop(uri, None)

    def set_default_collection(self, members: Any) -> None:
        """
        Registers the members used when fn:collection() is called with no
        arguments. Members are handled as in :meth:`add_collection`.
        """
        with self._lock:
            self._default_collection = list(members)
            self._default_collection_nodes = None

    def get_document(self, uri: str, base_uri: Optional[str] = None) -> DocumentNode:
        """
        Returns the document node for a URI. The same absolute URI always
        maps to the same node identity within the context.

        :param uri: the URI of the document, resolved against the provided \
        or the context base URI if relative.
        :param base_uri: an optional explicit base URI.
        :raises ResourceError: if the URI is not registered or the resource \
        cannot be loaded.
        """
        uri = self.resolve_uri(uri, base_uri)
        with self._lock:
            try:
                return self._documents[uri]
            except KeyError:
                pass

            try:
                content = self._registry[uri]
            except KeyError:
                raise ResourceError('unknown document URI', uri) from None

            if content is None:
                if self.loader is None:
                    raise ResourceError('no loader available for document', uri)
                try:
                    content = self.loader(uri)
                except ResourceError:
                    raise
                except Exception as err:
                    raise ResourceError('cannot load document', uri) from err

            node = get_node_tree(content, self.namespaces, uri)
            document = node if isinstance(node, DocumentNode) \
                else node.get_document_node()
            self._documents[uri] = document
            return document

    def get_collection(self, uri: Optional[str] = None,
                       base_uri: Optional[str] = None) -> list[XPathNode]:
        """
        Returns the nodes of a registered collection, in registration order.
        Members repeated under the same URI are not duplicated: they resolve
        to the same node identity and are included only once.

        :param uri: the URI of the collection or `None` for the default \
        collection.
        :param base_uri: an optional explicit base URI.
        :raises ResourceError: if the collection is not registered.
        """
        if uri is None:
            with self._lock:
                if self._default_collection is None:
                    raise ResourceError('no default collection has been defined')
                if self._default_collection_nodes is None:
                    self._default_collection_nodes = self._build_members(
                        self._default_collection, base_uri
                    )
                return list(self._default_collection_nodes)

        uri = self.resolve_uri(uri, base_uri)
        with self._lock:
            try:
                members = self._collections[uri]
            except KeyError:
                raise ResourceError('collection not found', uri) from None

            try:
                return list(self._collection_nodes[uri])
            except KeyError:
                nodes = self._build_members(members, base_uri)
                self._collection_nodes[uri] = nodes
                return list(nodes)

    def _build_members(self, members: list[Any],
                       base_uri: Optional[str]) -> list[XPathNode]:
        nodes: list[XPathNode] = []
        for member in members:
            if isinstance(member, str):
                node: XPathNode = self.get_document(member, base_uri)
            elif isinstance(member, XPathNode):
                node = member
            else:
                node = get_node_tree(member, self.namespaces)

            if not any(node is x for x in nodes):
                nodes.append(node)

        return nodes
