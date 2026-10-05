#
# Copyright (c), 2022-2026, SISSA (International School for Advanced Studies).
# All rights reserved.
# This file is distributed under the terms of the MIT License.
# See the file 'LICENSE' in the root directory of the present
# distribution, or http://opensource.org/licenses/MIT.
#
# @author Davide Brunato <brunato@sissa.it>
#
import locale
import threading
from abc import ABCMeta, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Optional, Union
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


#
# Application-defined collation protocol
#
ComparisonUnit = Union[Any, tuple[Any, ...]]


@dataclass(frozen=True)
class CollationElement:
    """
    A single comparison unit of a collation decomposition.

    Each element carries the *unit* used to compare it and the portion of
    the original string it derives from, as the half-open source span
    ``[start, end)``. This span mapping is what permits finding the
    boundaries of a match in the original string even when the collation
    ignores characters (an element never spans an ignored character) or
    expands a single character into more than one comparison unit (the
    generated elements share the same source span).
    """
    unit: ComparisonUnit
    start: int
    end: int


class AbstractCollation(metaclass=ABCMeta):
    """
    A custom collation, scoped to an XPath parser/static context.

    An implementation provides the collation *sorting keys* through
    :meth:`analyze` (or :meth:`sort_key`) and, optionally, the *matching
    ranges* used by ``contains``, ``starts-with``, ``ends-with`` and
    ``substring-before``/``substring-after``.

    A collation that can only order strings has to set :attr:`substring_matching`
    to ``False``: substring matching functions are then rejected with the
    ``FOCH0002`` XPath error, as required by the specification.
    """

    #: Tells whether the collation defines a matching semantics, not only
    #: a comparison semantics. Collations based on opaque ordering keys may
    #: not be able to determine whether a key is a prefix/subkey of another.
    substring_matching: bool = True

    @abstractmethod
    def analyze(self, s: str) -> list[CollationElement]:
        """
        Decomposes a string into collation elements.

        Ignored characters generate no element and must not be included
        in the source span of any element; a single character can produce
        multiple elements (expansion), all sharing the same source span.
        """

    def sort_key(self, s: str) -> tuple[ComparisonUnit, ...]:
        """Returns the sorting key of *s* as a sequence of comparison units."""
        return tuple(elem.unit for elem in self.analyze(s))

    def compare(self, s1: str, s2: str) -> int:
        """Compares two strings lexicographically on their sorting keys."""
        key1 = self.sort_key(s1)
        key2 = self.sort_key(s2)
        if key1 == key2:
            return 0
        return -1 if key1 < key2 else 1

    def eq(self, s1: str, s2: str) -> bool:
        return self.sort_key(s1) == self.sort_key(s2)

    def match_range(self, source: str, target: str,
                    start: int, end: Optional[int] = None) -> Optional[tuple[int, int]]:
        """
        Returns the half-open ``[start, end)`` span in the original *source*
        string of the first contiguous run of collation elements matching
        *target*, or ``None`` if *target* does not match at that position.

        *start* is the index in the element list of *source* where the match
        has to begin; *end* optionally restricts the search of the matching
        run (used by ``ends-with``). The returned span refers to positions in
        the original string, so callers slice the original string and never
        the folded one.
        """
        elements = self.analyze(source)
        if end is None:
            end = len(elements)

        target_elements = self.analyze(target)
        n = len(target_elements)
        if n == 0:
            # Every position matches the empty collation: the span is empty.
            if start < end:
                return elements[start].start, elements[start].start
            return len(source), len(source)

        stop = end - n
        if start > stop:
            return None

        for index in range(start, stop + 1):
            chunk = elements[index:index + n]
            if all(chunk[k].unit == target_elements[k].unit for k in range(n)):
                return chunk[0].start, chunk[-1].end
        return None

    def contains(self, source: str, target: str) -> bool:
        return self.match_range(source, target, 0) is not None

    def starts_with(self, source: str, target: str) -> bool:
        return self.match_range(source, target, 0) is not None

    def ends_with(self, source: str, target: str) -> bool:
        elements = self.analyze(source)
        target_elements = self.analyze(target)
        n = len(target_elements)
        if n == 0:
            return True
        if n > len(elements):
            return False
        return self.match_range(source, target, len(elements) - n, len(elements)) is not None

    def find(self, source: str, target: str) -> int:
        """
        Returns the starting position of *target* in the original *source*
        string, or ``-1`` if there is no match.
        """
        result = self.match_range(source, target, 0)
        return -1 if result is None else result[0]

    def substring_before(self, source: str, target: str) -> str:
        if target == '':
            # XPath: an empty second argument always matches at position 1,
            # so the substring before it is the empty string.
            return ''
        result = self.match_range(source, target, 0)
        return source[:result[0]] if result is not None else ''

    def substring_after(self, source: str, target: str) -> str:
        if target == '':
            # XPath: the substring after an empty match is the whole argument
            return source
        result = self.match_range(source, target, 0)
        return source[result[1]:] if result is not None else ''


class CodepointCollation(AbstractCollation):
    """The Unicode Codepoint Collation (identity per character)."""

    substring_matching = True

    def analyze(self, s: str) -> list[CollationElement]:
        return [CollationElement(c, index, index + 1) for index, c in enumerate(s)]

    def sort_key(self, s: str) -> tuple[ComparisonUnit, ...]:
        return tuple(s)

    def compare(self, s1: str, s2: str) -> int:
        return unicode_codepoint_strcoll(s1, s2)

    def eq(self, s1: str, s2: str) -> bool:
        return s1 == s2


CODEPOINT_COLLATION = CodepointCollation()


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


def unicode_codepoint_strcoll(s1: str, s2: str) -> int:
    return 0 if s1 == s2 else -1 if s1 < s2 else 1


def unicode_codepoint_strxfrm(s: str) -> str:
    return s


def case_insensitive_strcoll(s1: str, s2: str) -> int:
    if s1.casefold() == s2.casefold():
        return 0
    elif s1.casefold() < s2.casefold():
        return -1
    else:
        return 1


def case_insensitive_strxfrm(s: str) -> str:
    return s.casefold()


class CollationManager(context_class_base):
    """
    Context Manager for collations. Provide helper operators as methods.
    """
    lc_collate: Union[None, str, tuple[Optional[str], Optional[str]]]
    fallback: bool = False
    _current_lc_collate: Optional[tuple[Optional[str], Optional[str]]] = None

    def __init__(self,
                 collation: Optional[str],
                 token: Optional['XPathToken'] = None,
                 custom_collations:
                     Optional[dict[str, AbstractCollation]] = None) -> None:
        self.collation = collation
        self.token = token
        self.strcoll = locale.strcoll
        self.strxfrm = locale.strxfrm
        self.custom_collation: Optional[AbstractCollation] = None

        if collation is None:
            msg = 'collation cannot be an empty sequence'
            raise xpath_error('XPTY0004', msg, self.token)
        elif not urlsplit(collation).scheme and token is not None:
            # Collation is a relative URI: try to complete with the static base URI
            base_uri = token.parser.base_uri
            if base_uri:
                collation = urljoin(base_uri, collation)

        if custom_collations is None and token is not None:
            custom_collations = getattr(token.parser, 'collations', None)

        if custom_collations is not None and collation in custom_collations:
            # An application-registered collation: pure Python, no locale change
            self.lc_collate = None
            self.custom_collation = custom_collations[collation]
            self.strcoll = self.custom_collation.compare
            self.strxfrm = self.custom_collation.sort_key
        elif collation == UNICODE_CODEPOINT_COLLATION:
            self.lc_collate = None
            self.strcoll = unicode_codepoint_strcoll
            self.strxfrm = unicode_codepoint_strxfrm
        elif collation == HTML_ASCII_CASE_INSENSITIVE_COLLATION:
            self.lc_collate = None
            self.strcoll = case_insensitive_strcoll
            self.strxfrm = case_insensitive_strxfrm
        elif collation == XQUERY_TEST_SUITE_CASEBLIND_COLLATION:
            self.lc_collate = None
            self.strcoll = case_insensitive_strcoll
            self.strxfrm = case_insensitive_strxfrm
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

    def _require_substring_matching(self) -> None:
        if self.custom_collation is not None                 and not self.custom_collation.substring_matching:
            msg = f"collation {self.collation!r} does not support substring matching"
            raise xpath_error('FOCH0002', msg, self.token)

    def eq(self, a: Any, b: Any) -> bool:
        if not isinstance(a, str) or not isinstance(b, str):
            return bool(a == b)
        return self.strcoll(a, b) == 0

    def ne(self, a: Any, b: Any) -> bool:
        if not isinstance(a, str) or not isinstance(b, str):
            return bool(a != b)
        return self.strcoll(a, b) != 0

    def contains(self, a: str, b: str) -> bool:
        if self.custom_collation is not None:
            self._require_substring_matching()
            return self.custom_collation.contains(a, b)
        return self.strxfrm(b) in self.strxfrm(a)

    def find(self, a: str, b: str) -> int:
        if self.custom_collation is not None:
            self._require_substring_matching()
            return self.custom_collation.find(a, b)
        return self.strxfrm(a).find(self.strxfrm(b))

    def startswith(self, a: str, b: str) -> bool:
        if self.custom_collation is not None:
            self._require_substring_matching()
            return self.custom_collation.starts_with(a, b)
        return self.strxfrm(a).startswith(self.strxfrm(b))

    def endswith(self, a: str, b: str) -> bool:
        if self.custom_collation is not None:
            self._require_substring_matching()
            return self.custom_collation.ends_with(a, b)
        return self.strxfrm(a).endswith(self.strxfrm(b))

    def substring_before(self, a: str, b: str) -> str:
        if self.custom_collation is None:
            index = self.find(a, b)
            return a[:index] if index >= 0 else ''
        self._require_substring_matching()
        return self.custom_collation.substring_before(a, b)

    def substring_after(self, a: str, b: str) -> str:
        if self.custom_collation is None:
            index = self.find(a, b)
            return a[index + len(b):] if index >= 0 else ''
        self._require_substring_matching()
        return self.custom_collation.substring_after(a, b)
