#
# Copyright (c), 2022-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
"""
Collation support.

A *collation* defines how strings are compared and, optionally, how substrings are
matched. Collations are addressed in XPath expressions through an absolute URI and
are resolved locally by the parser (the static context), so registering a custom
collation never alters the process global locale.

A collation is provided as a :class:`Collation` subclass instance:

* a collation that only implements :meth:`Collation.sort_key` (or ``strcoll``) is a
  *compare-only* collation: ``fn:compare``, value/general comparisons and every
  equality based function (``fn:distinct-values``, ``fn:index-of``,
  ``fn:deep-equal``, ``fn:contains-token`` ...) keep working, but the substring
  matching functions (``fn:contains``, ``fn:starts-with``, ``fn:ends-with``,
  ``fn:substring-before`` and ``fn:substring-after``) reject it with ``FOCH0002``;
* a collation that implements :meth:`MatchingCollation.iter_units` supplies a
  *matching range* protocol and can be used with the substring matching functions.

The unit protocol returned by ``iter_units`` represents the decomposition of a
string into *collation units*:

* a character that must be ignored (e.g. a separator) yields one or more units
  with an empty ``key`` tuple. These units never take part in matching but still
  delimit the portion of the *original* string that is returned;
* a character can expand into several comparison units (e.g. 'ß' expanded into
  's', 's'), so a unit is a slice ``[start, end)`` of the original string paired
  with a tuple of atomic sort-key elements.

Matching is performed on the flattened element stream but a match can neither
split a source unit nor start/end in the middle of one. The returned slice always
refers to the original string, never to a folded/normalized copy.
"""
import locale
import threading
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, Optional, Union
from urllib.parse import urljoin, urlsplit

from elementpath.exceptions import xpath_error

if TYPE_CHECKING:
    from .xpath_tokens import XPathToken
    context_class_base = AbstractContextManager[Any]
else:
    context_class_base = AbstractContextManager

UNICODE_COLLATION_BASE_URI = "http://www.w3.org/2013/collation/UCA"

UNICODE_CODEPOINT_COLLATION = \
    "http://www.w3.org/2005/xpath-functions/collation/codepoint"

HTML_ASCII_CASE_INSENSITIVE_COLLATION = \
    "http://www.w3.org/2005/xpath-functions/collation/html-ascii-case-insensitive"

XQUERY_TEST_SUITE_CASEBLIND_COLLATION = \
    "http://www.w3.org/2010/09/qt-fots-catalog/collation/caseblind"

# Alias for sort-key elements: any comparable/hashable atomic object is allowed
SortKeyElement = Any


@dataclass(frozen=True)
class CollationUnit:
    """
    A single collation unit: the slice ``[start, end)`` of the original string
    that produced it together with the atomic elements of its sort key.

    An empty ``key`` tuple marks an *ignorable* unit (a character that is not
    compared nor matched, like a configured separator).
    """
    start: int
    end: int
    key: tuple[SortKeyElement, ...]


class MatchRange(NamedTuple):
    """The span ``[start, end)`` of the original string covered by a match."""
    start: int
    end: int


def _flatten_units(units: list[CollationUnit]) \
        -> tuple[list[SortKeyElement], list[int], list[int]]:
    """
    Flattens collation units into a sequence of atomic sort-key elements, keeping
    the boundary of the source unit of each element.

    :returns: the element stream and, for each element position, the start and
    the end offset (in the original string) of the unit it belongs to.
    """
    elements: list[SortKeyElement] = []
    starts: list[int] = []
    ends: list[int] = []
    for unit in units:
        for element in unit.key:
            elements.append(element)
            starts.append(unit.start)
            ends.append(unit.end)
    return elements, starts, ends


class Collation(ABC):
    """
    Abstract compare-only collation.

    A concrete collation has at least to provide a *sort key* for every string:
    two strings compare equal when their sort keys compare equal, otherwise the
    usual lexicographic ordering of the keys applies. A collation that cannot
    provide substring matching semantics stays a plain :class:`Collation` and is
    rejected by the substring matching functions with ``FOCH0002``.
    """

    #: Whether the collation can decompose strings for substring matching.
    supports_matching: ClassVar[bool] = False

    @abstractmethod
    def sort_key(self, text: str) -> tuple[SortKeyElement, ...]:
        """Returns the sort key of *text* as a tuple of comparable elements."""

    def strcoll(self, s1: str, s2: str) -> int:
        """Compares two strings, returning -1, 0 or 1 like :func:`locale.strcoll`."""
        k1 = self.sort_key(s1)
        k2 = self.sort_key(s2)
        return 0 if k1 == k2 else -1 if k1 < k2 else 1

    def strxfrm(self, text: str) -> str:
        """
        Returns a transform of *text* such that ordering the transformed strings
        with the code point order equals ordering the originals with the
        collation. The default joins the sort-key elements with ``'\\x00'`` and
        works when every element is a string; override it when needed.
        """
        return '\x00'.join(self.sort_key(text))


class MatchingCollation(Collation, ABC):
    """
    A collation that also provides substring matching semantics.

    The :meth:`iter_units` method yields the collation units of a string. Each
    unit is a slice of the original string paired with a (possibly empty) tuple
    of atomic sort-key elements: an empty tuple means an ignored character and a
    multi-element tuple means a one-to-many expansion (e.g. 'ß' → 's', 's').
    """

    supports_matching: ClassVar[bool] = True

    @abstractmethod
    def iter_units(self, text: str):
        """Yields the :class:`CollationUnit` decomposition of *text*."""
        raise NotImplementedError

    def units(self, text: str) -> list[CollationUnit]:
        return list(self.iter_units(text))

    def sort_key(self, text: str) -> tuple[SortKeyElement, ...]:
        return tuple(element for unit in self.iter_units(text)
                     for element in unit.key)


class CodePointCollation(MatchingCollation):
    """The Unicode code point collation: every code point compares with itself."""

    def strxfrm(self, text: str) -> str:
        return text

    def iter_units(self, text: str):
        start = 0
        for char in text:
            end = start + len(char)
            yield CollationUnit(start, end, (char,))
            start = end


class CaseInsensitiveCollation(MatchingCollation):
    """
    Case-insensitive collation used for the HTML ASCII case-insensitive and the
    XQuery test suite caseblind built-in collation URIs.
    """

    def strxfrm(self, text: str) -> str:
        return text.casefold()

    def iter_units(self, text: str):
        start = 0
        for char in text:
            end = start + len(char)
            folded = char.casefold()
            yield CollationUnit(start, end, tuple(folded))
            start = end


# Built-in collation singletons
_CODEPOINT_COLLATION = CodePointCollation()
_CASE_INSENSITIVE_COLLATION = CaseInsensitiveCollation()


def _iter_minimal_matches(haystack: list[CollationUnit],
                          needle: list[CollationUnit]) -> list[MatchRange]:
    """
    Finds every minimal span of *haystack* whose non-ignorable sort-key
    elements equal those of *needle*.

    A match is a contiguous span of collation units: ignored units of the
    haystack that lie *between* the first and the last matched unit are part of
    the span, while leading/trailing ignored units are not. A source collation
    unit can never be split: the match starts at the first element of a unit
    and consumes the whole unit it ends in (e.g. the two elements produced by a
    '\u00df' expansion are matched together, so a single 's' cannot match half
    of it).
    """
    needle_elements, _, _ = _flatten_units(needle)
    if not needle_elements:
        return []

    h_elements, h_starts, h_ends = _flatten_units(haystack)
    n = len(h_elements)
    m = len(needle_elements)

    # For each flattened element index, the element index that begins the
    # next non-ignorable source unit (n marks the end of the stream).
    next_unit_start: dict[int, int] = {}
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for unit in haystack:
        if unit.key:
            boundaries.append((cursor, cursor + len(unit.key)))
            cursor += len(unit.key)
    for index, (start, end) in enumerate(boundaries):
        following = boundaries[index + 1][0] if index + 1 < len(boundaries) else n
        for element in range(start, end):
            next_unit_start[element] = following

    matches: list[MatchRange] = []
    for offset in range(n - m + 1):
        for k in range(m):
            if h_elements[offset + k] != needle_elements[k]:
                break
        else:
            last = offset + m - 1
            starts_unit = next_unit_start[offset - 1] == offset if offset else True
            ends_at_unit_boundary = last + 1 == next_unit_start[last]
            if not starts_unit or not ends_at_unit_boundary:
                continue
            matches.append(MatchRange(h_starts[offset], h_ends[last]))
    return matches


def _maximal_end(units: list[CollationUnit], last_end: int) -> int:
    """Extends the end of a match across following ignorable units."""
    end = last_end
    for unit in units:
        if unit.start < end:
            continue
        elif unit.key:
            break
        else:
            end = unit.end
    return end


def _has_only_ignorable_before(units: list[CollationUnit], position: int) -> bool:
    """True when every unit strictly before *position* is ignorable."""
    return all(not unit.key for unit in units if unit.start < position)


def _has_only_ignorable_after(units: list[CollationUnit], position: int) -> bool:
    """True when every unit strictly after *position* is ignorable."""
    return all(not unit.key for unit in units if unit.end > position)


_locale_collate_lock = threading.Lock()


def get_locale_category(category: int) -> str:
    """
    Gets the current value of a locale category. A replacement
    of locale.getdefaultlocale(), deprecated since Python 3.11.
    """
    _locale = locale.setlocale(category, None)
    if _locale == 'C':
        # locale category does not seem to be configured, so get the user
        # preferred locale and then restore the  previous state
        _locale = locale.setlocale(category, '')
        locale.setlocale(category, 'C')

    return _locale


#: Built-in non locale based collations
_BUILTIN_COLLATIONS: dict[str, Collation] = {
    UNICODE_CODEPOINT_COLLATION: _CODEPOINT_COLLATION,
    HTML_ASCII_CASE_INSENSITIVE_COLLATION: _CASE_INSENSITIVE_COLLATION,
    XQUERY_TEST_SUITE_CASEBLIND_COLLATION: _CASE_INSENSITIVE_COLLATION,
}


class CollationManager(context_class_base):
    """
    Context Manager for collations. Resolves a collation URI against the
    collations registered on the parser (the static context), then against the
    built-in collations, and finally against locale based collations.

    Locale based collations only provide comparison semantics (``strcoll`` /
    ``strxfrm``); the substring matching functions reject them with ``FOCH0002``.
    """
    lc_collate: Union[None, str, tuple[Optional[str], Optional[str]]]
    fallback: bool = False
    _current_lc_collate: Optional[tuple[Optional[str], Optional[str]]] = None

    def __init__(self,
                 collation: Optional[str],
                 token: Optional['XPathToken'] = None) -> None:
        self.collation = collation
        self.token = token
        self.collation_obj: Optional[Collation] = None
        self.strcoll = locale.strcoll
        self.strxfrm = locale.strxfrm

        if collation is None:
            msg = 'collation cannot be an empty sequence'
            raise xpath_error('XPTY0004', msg, self.token)
        elif not urlsplit(collation).scheme and token is not None:
            # Collation is a relative URI: try to complete with the static base URI
            base_uri = token.parser.base_uri
            if base_uri:
                collation = urljoin(base_uri, collation)
                self.collation = collation

        parser_collations = getattr(
            getattr(token, 'parser', None), 'collations', None
        )
        if parser_collations is not None and collation in parser_collations:
            # Collation registered on the parser (static context): no locale use
            self.collation_obj = parser_collations[collation]
            self.lc_collate = None
            self.strcoll = self.collation_obj.strcoll
            self.strxfrm = self.collation_obj.strxfrm
        elif collation in _BUILTIN_COLLATIONS:
            self.collation_obj = _BUILTIN_COLLATIONS[collation]
            self.lc_collate = None
            self.strcoll = self.collation_obj.strcoll
            self.strxfrm = self.collation_obj.strxfrm
        elif collation.startswith(UNICODE_COLLATION_BASE_URI):
            self.lc_collate = 'en_US.UTF-8'
            self.fallback = True
            for param in urlsplit(collation).query.split(';'):
                assert isinstance(param, str)
                if param.startswith('lang='):
                    # Language code: should be a string in lexical space of xs:language,
                    # but in implementations '_' can be used instead of hyphens and '.'
                    # is used to provide the encoding. Use UTF-8 as default encoding.
                    lang = param[5:]
                    self.lc_collate = lang if '.' in lang else (lang, 'UTF-8')
                elif param.startswith('fallback='):
                    if param.endswith('yes'):
                        self.fallback = True
                    elif param.endswith('no'):
                        self.fallback = False
        else:
            # Other compatible collations locale lib specs (e.g.: it_IT.UTF-8)
            self.lc_collate = collation

    def __enter__(self) -> 'CollationManager':
        if self.lc_collate is not None:
            # Only one locale set can be used at a time
            _locale_collate_lock.acquire()
            self._current_lc_collate = locale.getlocale(locale.LC_COLLATE)

            try:
                locale.setlocale(locale.LC_COLLATE, self.lc_collate)
            except locale.Error:
                if not self.fallback:
                    self._current_lc_collate = None
                    _locale_collate_lock.release()

                    msg = f"Unsupported collation {self.collation!r}"
                    raise xpath_error('FOCH0002', msg, self.token) from None

                locale.setlocale(locale.LC_COLLATE, 'en_US.UTF-8')

        return self

    def __exit__(self, exc_type: Optional[type[BaseException]],
                 exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> None:
        if self._current_lc_collate is not None:
            locale.setlocale(locale.LC_COLLATE, self._current_lc_collate)
            self._current_lc_collate = None
            _locale_collate_lock.release()

    @property
    def supports_matching(self) -> bool:
        return self.collation_obj is not None and self.collation_obj.supports_matching

    def _check_matching(self) -> MatchingCollation:
        if not self.supports_matching or not isinstance(self.collation_obj,
                                                        MatchingCollation):
            msg = f"collation {self.collation!r} does not support substring matching"
            raise xpath_error('FOCH0002', msg, self.token)
        return self.collation_obj

    def eq(self, a: Any, b: Any) -> bool:
        if not isinstance(a, str) or not isinstance(b, str):
            return bool(a == b)
        return self.strcoll(a, b) == 0

    def ne(self, a: Any, b: Any) -> bool:
        if not isinstance(a, str) or not isinstance(b, str):
            return bool(a != b)
        return self.strcoll(a, b) != 0

    def _match_range(self, text: str, substring: str, *,
                     maximal: bool = False, allow_leading: bool = False,
                     allow_trailing: bool = False) -> Optional[MatchRange]:
        if substring == '':
            # Empty substring matches at position 0: substring-before returns
            # '' (text[:0]) and substring-after returns the whole text (text[0:]).
            # This result is collation independent, so it is computed before
            # checking that the collation supports substring matching.
            return MatchRange(0, 0)

        collation = self._check_matching()
        text_units = collation.units(text)
        substring_units = collation.units(substring)
        matches = _iter_minimal_matches(text_units, substring_units)
        if not matches:
            return None

        if allow_leading:
            valid = [m for m in matches
                     if _has_only_ignorable_before(text_units, m.start)]
            match = valid[0] if valid else None
        elif allow_trailing:
            valid = [m for m in matches
                     if _has_only_ignorable_after(text_units, m.end)]
            match = valid[-1] if valid else None
        else:
            match = matches[0]

        if match is None:
            return None
        elif maximal:
            return MatchRange(match.start, _maximal_end(text_units, match.end))
        return match

    def contains(self, text: str, substring: str) -> bool:
        if substring == '':
            return True
        return self._match_range(text, substring, maximal=False) is not None

    def match_range(self, text: str, substring: str, *,
                    maximal: bool = False) -> Optional[MatchRange]:
        """
        Returns the ``[start, end)`` span of the *original* *text* matched by
        *substring*, or `None` when there is no match. The span is minimal for
        ``fn:substring-before`` and maximal (trailing ignored units included)
        for ``fn:substring-after``.
        """
        return self._match_range(text, substring, maximal=maximal)

    def startswith(self, text: str, prefix: str) -> bool:
        if prefix == '':
            return True
        match_range = self._match_range(text, prefix, allow_leading=True)
        return match_range is not None

    def endswith(self, text: str, suffix: str) -> bool:
        if suffix == '':
            return True
        match_range = self._match_range(text, suffix, allow_trailing=True)
        return match_range is not None
