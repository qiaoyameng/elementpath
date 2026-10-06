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
Explicit resource resolution context for the XPath functions fn:doc,
fn:doc-available and fn:collection.

Documents and collections are made available to XPath expressions only
through a resolver instance, where the calling application registers the
allowed URIs, the collection members and optional loader functions. No
default file or network access is provided: a URI that is not registered
and cannot be loaded by a registered loader is rejected.
"""
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

import elementpath.aliases as ta

from elementpath.exceptions import ElementPathResourceError, ElementPathTypeError
from elementpath.tree_builders import get_node_tree
from elementpath.xpath_nodes import ElementNode, DocumentNode

__all__ = ['ResourceResolver', 'normalize_uri', 'sanitize_uri']

#: A callable that loads a resource from an absolute URI, returning a root
#: object (an Element, an ElementTree or a DocumentNode) or `None` if the
#: URI is not handled by the loader.
LoaderType = Callable[[str], Any]


def _remove_dot_segments(path: str) -> str:
    """Removes dot segments from a URI path, as defined by RFC 3986 section 5.2.4."""
    segments: list[str] = []
    for segment in path.split('/'):
        if segment == '.':
            continue
        elif segment == '..':
            if segments and segments[-1] != '..':
                segments.pop()
            elif not path.startswith('/'):
                segments.append(segment)
        else:
            segments.append(segment)

    normalized = '/'.join(segments)
    if path.endswith(('/.', '/..')) and not normalized.endswith('/'):
        normalized += '/'
    return normalized


def normalize_uri(uri: str, base_uri: Optional[str] = None) -> str:
    """
    Normalizes a URI, resolving it against an optional base URI.

    :param uri: a URI or a relative reference.
    :param base_uri: an optional base URI used to resolve relative references.
    :returns: the normalized absolute URI, or the normalized argument if \
    no base URI is available for resolving a relative reference.
    """
    uri = uri.strip()
    parts = urlsplit(uri)
    if not parts.scheme and not parts.netloc and base_uri:
        parts = urlsplit(urljoin(base_uri, uri))

    path = _remove_dot_segments(parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc, path,
                       parts.query, parts.fragment))


def sanitize_uri(uri: str) -> str:
    """
    Returns a URI string safe to include in error messages, with the
    userinfo part (credentials) removed from the authority component.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        return uri

    if parts.username is None and parts.password is None:
        return uri

    try:
        port = parts.port
    except ValueError:
        port = None

    host = parts.hostname or ''
    netloc = host if port is None else f'{host}:{port}'
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class ResourceResolver:
    """
    An explicit resource resolution context for fn:doc, fn:doc-available
    and fn:collection.

    The calling application registers the allowed document URIs, the
    collections and optional loader functions. XPath expressions can only
    access the resources exposed by the resolver instance associated with
    the dynamic context; unknown URIs are rejected and no implicit file
    or network access is attempted.

    Document nodes are cached per resolver instance, so the same absolute
    URI always maps to the same document node identity for all the
    evaluations that share the resolver. Distinct resolver instances have
    independent caches.

    :param documents: an optional mapping of URIs to document roots. A \
    `None` value marks the URI as allowed but to be loaded on demand \
    through the registered loaders.
    :param collections: an optional mapping of URIs to collection members. \
    Each member is either a URI string (resolved through the document \
    cache, so node identity is shared with fn:doc) or a document root.
    :param default_collection: an optional sequence of members used when \
    fn:collection is called with no arguments.
    :param loaders: an optional iterable of loader callables, tried in \
    order for resolving allowed URIs that have no registered document.
    :param base_uri: an optional base URI used to normalize the URIs \
    registered with the resolver.
    """
    def __init__(self,
                 documents: Optional[dict[str, ta.RootArgType]] = None,
                 collections: Optional[dict[str, Sequence[Any]]] = None,
                 default_collection: Optional[Sequence[Any]] = None,
                 loaders: Iterable[LoaderType] = (),
                 base_uri: Optional[str] = None) -> None:

        self.base_uri = base_uri
        self._documents: dict[str, ta.RootArgType] = {}
        self._collections: dict[str, list[Any]] = {}
        self._default_collection: Optional[list[Any]] = None
        self._loaders: list[LoaderType] = []
        self._document_cache: dict[str, DocumentNode] = {}
        self._collection_cache: dict[str, list[DocumentNode]] = {}

        if documents is not None:
            for uri, document in documents.items():
                self.register_document(uri, document)

        if collections is not None:
            for uri, members in collections.items():
                self.register_collection(uri, members)

        if default_collection is not None:
            self.register_collection(None, default_collection)

        for loader in loaders:
            self.register_loader(loader)

    def __repr__(self) -> str:
        return '%s(documents=%d, collections=%d, loaders=%d)' % (
            self.__class__.__name__, len(self._documents),
            len(self._collections), len(self._loaders)
        )

    def resolve_uri(self, uri: str, base_uri: Optional[str] = None) -> str:
        """
        Resolves a URI reference against the static base URI and normalizes it.

        :param uri: a URI or a relative reference.
        :param base_uri: the static base URI; if not provided the base URI \
        of the resolver is used.
        """
        return normalize_uri(uri, base_uri or self.base_uri)

    def register_document(self, uri: str,
                          document: Optional[ta.RootArgType] = None) -> None:
        """
        Registers an allowed document URI.

        :param uri: the document URI, normalized against the resolver base URI.
        :param document: the document root, or `None` if the document has \
        to be loaded on demand through the registered loaders.
        """
        key = self.resolve_uri(uri)
        self._documents[key] = document
        self._document_cache.pop(key, None)

    def register_collection(self, uri: Optional[str], members: Sequence[Any]) -> None:
        """
        Registers a collection with a stable member order.

        :param uri: the collection URI, or `None` for registering the \
        default collection.
        :param members: a sequence of members; each member is either a URI \
        string or a document root. Repeated URIs are kept in place and \
        share the same cached document node.
        """
        if uri is None:
            self._default_collection = list(members)
        else:
            key = self.resolve_uri(uri)
            self._collections[key] = list(members)
            self._collection_cache.pop(key, None)

    def register_loader(self, loader: LoaderType) -> None:
        """
        Registers a loader function for loading allowed resources on demand.
        A loader is called with the normalized absolute URI and has to return
        a document root, or `None` if it does not handle the URI.
        """
        if not callable(loader):
            raise ElementPathTypeError(f"{loader!r} is not a callable")
        elif loader not in self._loaders:
            self._loaders.append(loader)

    def clear_cache(self) -> None:
        """Clears the document and collection node caches of the resolver."""
        self._document_cache.clear()
        self._collection_cache.clear()

    def _build_document(self, uri: Optional[str], value: ta.RootArgType) -> DocumentNode:
        if isinstance(value, DocumentNode):
            if uri is not None and value.uri is None:
                value.tree.uri = uri
            return value

        node = get_node_tree(value, uri=uri, fragment=False)
        if isinstance(node, DocumentNode):
            return node
        elif isinstance(node, ElementNode):
            return node.get_document_node()

        msg = f"invalid document root {value!r} for resource {sanitize_uri(uri or '')!r}"
        raise ElementPathTypeError(msg)

    def _load_document(self, key: str) -> DocumentNode:
        try:
            value = self._documents[key]
        except KeyError:
            value = None
        else:
            if value is not None:
                return self._build_document(key, value)

        for loader in self._loaders:
            try:
                loaded = loader(key)
            except Exception as err:
                msg = "cannot load resource {!r}: {}".format(
                    sanitize_uri(key), type(err).__name__
                )
                raise ElementPathResourceError(msg, code='FODC0002') from err

            if loaded is not None:
                return self._build_document(key, loaded)

        if key in self._documents:
            msg = f"document {sanitize_uri(key)!r} is not available"
        else:
            msg = f"unknown document URI {sanitize_uri(key)!r}"
        raise ElementPathResourceError(msg, code='FODC0002')

    def get_document(self, uri: str, base_uri: Optional[str] = None) -> DocumentNode:
        """
        Returns the document node for a URI, resolving relative references
        against the static base URI. The same absolute URI always returns
        the same document node instance for this resolver.

        :raises ElementPathResourceError: with code 'FODC0002' if the URI \
        is unknown or the resource cannot be loaded.
        """
        key = self.resolve_uri(uri, base_uri)
        try:
            return self._document_cache[key]
        except KeyError:
            document = self._load_document(key)
            self._document_cache[key] = document
            return document

    def get_collection(self, uri: Optional[str] = None,
                       base_uri: Optional[str] = None) -> list[DocumentNode]:
        """
        Returns the nodes of a collection, in stable registration order.
        Members registered by URI are resolved through the document cache,
        so repeated URIs and URIs shared with fn:doc map to the same node
        instances.

        :raises ElementPathResourceError: with code 'FODC0002' if the \
        collection URI is unknown or no default collection is defined.
        """
        if uri is None:
            if self._default_collection is None:
                msg = 'no default collection has been defined'
                raise ElementPathResourceError(msg, code='FODC0002')
            members = self._default_collection
            key = None
        else:
            key = self.resolve_uri(uri, base_uri)
            try:
                return self._collection_cache[key]
            except KeyError:
                pass

            try:
                members = self._collections[key]
            except KeyError:
                msg = f"unknown collection URI {sanitize_uri(key)!r}"
                raise ElementPathResourceError(msg, code='FODC0002') from None

        collection = []
        for member in members:
            if isinstance(member, str):
                collection.append(self.get_document(member, base_uri))
            else:
                collection.append(self._build_document(None, member))

        if key is not None:
            self._collection_cache[key] = collection
        return collection
