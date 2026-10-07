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
Streaming XPath selection over ElementTree ``iterparse`` events.
"""
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Optional
from xml.etree import ElementTree

from elementpath.aliases import NamespacesType, ParserClassType
from elementpath.exceptions import UnsupportedFeatureError
from elementpath.xpath2 import XPath2Parser
from elementpath.xpath_tokens.tokens import XPathToken

EventType = tuple[str, Any]
Predicate = Callable[[Any, int], bool]


def _unsupported(path: str, reason: str) -> UnsupportedFeatureError:
    return UnsupportedFeatureError(
        f"XPath expression {path!r} is not supported for streaming "
        f"selection: {reason}"
    )


@dataclass
class StreamStep:
    name: str
    predicates: tuple[Predicate, ...]
    positional: tuple[bool, ...]
    descendant: bool


@dataclass
class Source:
    step_index: int
    counters: list[int]


@dataclass
class Marker:
    step_index: int
    immediate_source: Optional[Source] = None


@dataclass
class Frame:
    element: Any
    parent: Optional[Any]
    direct_sources: list[Source]
    descendant_markers: list[Marker]
    enclosing_match: Optional['Frame']
    matched: bool
    matched_here: bool
    active_context: bool
    pending: list[tuple[Any, list[tuple[Any, list[Any]]]]]


def _literal_value(token: XPathToken) -> Any:
    if token.symbol == '(integer)':
        return int(token.value)
    if token.symbol == '(decimal)':
        return float(token.value)
    if token.symbol == '(string)':
        return str(token.value)
    if token.symbol == 'true':
        return True
    if token.symbol == 'false':
        return False
    raise _unsupported('', 'only literals and attribute comparisons are allowed')


def _attribute_name(token: XPathToken) -> str:
    if token.symbol != '@':
        raise _unsupported('', 'only attribute tests are allowed in predicates')
    child = token[0]
    if token.name:
        return token.name
    if child.symbol == ':':
        return child.name
    if child.symbol == '(name)':
        return str(child.value)
    raise _unsupported('', 'invalid attribute test')


def _uses_position(token: XPathToken) -> bool:
    return token.symbol == 'position' or any(
        _uses_position(child) for child in token
    )


def _validate_forward_expression(path: str, token: XPathToken) -> None:
    for node in token.iter():
        if node.symbol == 'last':
            raise _unsupported(path, "unbounded last() cannot be decided online")
        if node.label == 'axis' and node.symbol != 'child':
            reason = f'{node.symbol} axis requires document-order lookaround'
            raise _unsupported(path, reason)
        if node.symbol in ('following', 'following-sibling', 'preceding',
                           'preceding-sibling', 'parent',
                           'ancestor', 'ancestor-or-self'):
            raise _unsupported(path, f'{node.symbol} is not a forward streaming axis')


def _compile_attribute_predicate(path: str,
                                 token: XPathToken) -> tuple[bool, Predicate]:
    symbol = token.symbol

    if symbol == '(':
        return _compile_attribute_predicate(path, token[0])

    if symbol == 'true':
        return False, lambda elem, position: True
    if symbol == 'false':
        return False, lambda elem, position: False

    if symbol == '@':
        attribute_name = _attribute_name(token)
        return False, lambda elem, position: attribute_name in elem.attrib

    if symbol == 'not' and len(token) == 1 and token[0].symbol == '@':
        attribute_name = _attribute_name(token[0])
        return False, lambda elem, position: attribute_name not in elem.attrib

    if symbol in ('and', 'or'):
        left_positional, left = _compile_attribute_predicate(path, token[0])
        right_positional, right = _compile_attribute_predicate(path, token[1])
        if left_positional or right_positional:
            raise _unsupported(
                path, 'position predicates must be separate path predicates'
            )
        if symbol == 'and':
            predicate = lambda elem, position: \
                left(elem, position) and right(elem, position)
            return False, predicate
        predicate = lambda elem, position: \
            left(elem, position) or right(elem, position)
        return False, predicate

    if symbol == '(integer)':
        expected = int(token.value)
        return True, lambda elem, position: position == expected

    if symbol in ('=', '!=', '<', '<=', '>', '>='):
        left, right = token[0], token[1]

        if _uses_position(token):
            if left.symbol == 'position' and right.symbol == '(integer)':
                expected = int(right.value)
            elif right.symbol == 'position' and left.symbol == '(integer)':
                expected = int(left.value)
            else:
                raise _unsupported(path, 'position() must compare with an integer')

            def position_predicate(elem: Any, position: int) -> bool:
                if symbol == '=':
                    return position == expected
                if symbol == '!=':
                    return position != expected
                if symbol == '<':
                    return position < expected
                if symbol == '<=':
                    return position <= expected
                if symbol == '>':
                    return position > expected
                return position >= expected

            return True, position_predicate

        if left.symbol == '@' and right.label == 'literal':
            attribute_name = _attribute_name(left)
            expected = _literal_value(right)
        elif right.symbol == '@' and left.label == 'literal':
            attribute_name = _attribute_name(right)
            expected = _literal_value(left)
        else:
            raise _unsupported(
                path, 'predicate must compare an attribute with a literal'
            )

        if isinstance(expected, bool):
            def convert(value: str) -> bool:
                return value in ('1', 'true')
        elif isinstance(expected, (int, float)):
            def convert(value: str) -> float:
                return float(value)
        else:
            def convert(value: str) -> str:
                return value

        if symbol == '=':
            compare = lambda value: convert(value) == expected
        elif symbol == '!=':
            compare = lambda value: convert(value) != expected
        elif symbol == '<':
            compare = lambda value: convert(value) < expected
        elif symbol == '<=':
            compare = lambda value: convert(value) <= expected
        elif symbol == '>':
            compare = lambda value: convert(value) > expected
        else:
            compare = lambda value: convert(value) >= expected

        def attribute_predicate(elem: Any, position: int) -> bool:
            try:
                return compare(elem.attrib[attribute_name])
            except (KeyError, TypeError, ValueError):
                return False

        return False, attribute_predicate

    raise _unsupported(path, f'unsupported predicate {token.source!r}')


def _compile_predicate(path: str, token: XPathToken) -> tuple[bool, Predicate]:
    if _uses_position(token) and token.symbol == '(':
        raise _unsupported(path, 'unsupported position predicate')
    return _compile_attribute_predicate(path, token)


def _element_name(token: XPathToken) -> str:
    if token.symbol == '(name)':
        return token.name or str(token.value)
    if token.symbol == '*':
        return '*'
    if token.symbol == ':':
        return token.name
    raise _unsupported('', 'only element name tests and wildcards are supported')


def _split_element(path: str, token: XPathToken) -> XPathToken:
    while token.symbol == '[':
        token = token[0]
    if token.symbol not in ('(name)', '*', ':'):
        raise _unsupported(path, 'only forward element steps are supported')
    return token


def _split_predicates(token: XPathToken) -> list[XPathToken]:
    predicates: list[XPathToken] = []
    while token.symbol == '[':
        predicates.append(token[1])
        token = token[0]
    predicates.reverse()
    return predicates


def _tag_matches(name: str, tag: Any) -> bool:
    if not isinstance(tag, str):
        return False
    if name == '*' or name == tag:
        return True
    if name.startswith('{') and name.endswith('}*'):
        return tag.startswith(name[:-1])
    if name.startswith('{*}'):
        local_name = name[3:]
        return tag.startswith('{') and tag.endswith(f'}}{local_name}')
    return False


def _compile_path(path: str,
                  parser_class: ParserClassType,
                  namespaces: Optional[NamespacesType],
                  parser_kwargs: dict[str, Any]) -> tuple[bool, list[StreamStep]]:
    parser = parser_class(namespaces, **parser_kwargs)
    root_token = parser.parse(path)
    _validate_forward_expression(path, root_token)

    token = root_token
    raw_steps: list[tuple[bool, XPathToken]] = []
    absolute = False

    while token.symbol in ('/', '//'):
        is_unary = len(token) == 1
        right = token[0] if is_unary else token[1]
        raw_steps.append((token.symbol == '//', right))
        if is_unary:
            absolute = True
            token = None
            break
        token = token[0]

    if token is not None:
        if token.symbol == '.':
            if not raw_steps:
                raise _unsupported(path, 'an element path is required')
        else:
            raw_steps.append((False, token))

    if not raw_steps:
        raise _unsupported(path, 'an element path is required')

    raw_steps.reverse()
    steps: list[StreamStep] = []
    for index, (descendant, step_token) in enumerate(raw_steps):
        element = _split_element(path, step_token)
        predicate_tokens = _split_predicates(step_token)
        compiled = [
            _compile_predicate(path, predicate)
            for predicate in predicate_tokens
        ]
        steps.append(StreamStep(
            name=_element_name(element),
            predicates=tuple(predicate for _, predicate in compiled),
            positional=tuple(positional for positional, _ in compiled),
            descendant=descendant,
        ))

    return absolute, steps


def _clean_group(group: tuple[Any, list[tuple[Any, list[Any]]]],
                 queued: set[int]
                 ) -> Optional[tuple[Any, list[tuple[Any, list[Any]]]]]:
    element, nested = group
    children: list[tuple[Any, list[tuple[Any, list[Any]]]]] = []
    for child in nested:
        cleaned = _clean_group(child, queued)
        if cleaned is not None:
            children.append(cleaned)
    if id(element) in queued:
        return None
    queued.add(id(element))
    return element, children


def _yield_group(group: tuple[Any, list[tuple[Any, list[Any]]]],
                 delivered: set[int]) -> Iterator[Any]:
    element, nested = group
    if id(element) not in delivered:
        delivered.add(id(element))
        yield element
    for child_group in nested:
        yield from _yield_group(child_group, delivered)


def iter_select_events(events: Iterable[EventType],
                       path: str,
                       namespaces: Optional[NamespacesType] = None,
                       parser: Optional[ParserClassType] = None,
                       **kwargs: Any) -> Iterator[Any]:
    """
    Yield complete elements matched by a restricted XPath expression from events.

    Supported expressions are forward element paths with attribute comparisons and
    positional predicates that can be decided online, for example
    ``root/item[@kind="x"][2]`` and ``.//record[position() <= 10]``. Relative paths
    start from the stream's root element; absolute paths include the document root.
    Following, preceding, sibling, ancestor and unbounded ``last()`` expressions are
    rejected during compilation before *events* is consumed.

    A matched subtree is retained until its end event, then delivered. Other
    completed elements are detached from their parent unless they are inside an
    active match. Closing the returned generator closes *events* when possible and
    stops pulling from it.
    """
    parser_class = parser or XPath2Parser
    absolute, steps = _compile_path(path, parser_class, namespaces, kwargs)
    return _iter_select_events_impl(events, steps, absolute)


def _iter_select_events_impl(events: Iterable[EventType],
                             steps: list[StreamStep],
                             absolute: bool) -> Iterator[Any]:
    stack: list[Frame] = []
    delivered: set[int] = set()
    top_queue: set[int] = set()

    def fresh_source(step_index: int) -> Source:
        return Source(step_index, [0])

    def source_matches(source: Source, elem: Any) -> Optional[int]:
        step = steps[source.step_index]
        if not _tag_matches(step.name, elem.tag):
            return None

        for positional, predicate in zip(step.positional, step.predicates):
            if not positional and not predicate(elem, source.counters[0]):
                return None

        source.counters[0] += 1
        for positional, predicate in zip(step.positional, step.predicates):
            if positional and not predicate(elem, source.counters[0]):
                return None
        return source.step_index + 1

    try:
        for event, elem in events:
            if event not in ('start', 'end'):
                continue

            if event == 'start':
                parent_frame = stack[-1] if stack else None
                parent = parent_frame.element if parent_frame is not None else None
                direct_sources: list[Source] = []
                descendant_markers: list[Marker] = []
                matched_here = False
                final_match = False
                active_context = False
                enclosing_match: Optional[Frame] = None

                if parent_frame is None:
                    candidates: list[Source] = []
                    if absolute:
                        candidates.append(fresh_source(0))
                    if steps[0].descendant:
                        descendant_markers.append(Marker(0, fresh_source(0)))
                    elif not absolute:
                        direct_sources.append(fresh_source(0))
                else:
                    candidates = list(parent_frame.direct_sources)
                    for marker in parent_frame.descendant_markers:
                        candidates.append(marker.immediate_source)
                        source_for_grandchildren = fresh_source(marker.step_index)
                        descendant_markers.append(Marker(
                            marker.step_index, source_for_grandchildren
                        ))

                    enclosing_match = (
                        parent_frame if parent_frame.matched
                        or parent_frame.active_context
                        else parent_frame.enclosing_match
                    )

                unique_candidates: list[Source] = []
                for candidate in candidates:
                    if not any(candidate is item for item in unique_candidates):
                        unique_candidates.append(candidate)
                unique_candidates.sort(
                    key=lambda source: source.step_index, reverse=True
                )
                for source in unique_candidates:
                    next_index = source_matches(source, elem)
                    if next_index is None:
                        continue

                    step = steps[source.step_index]
                    if next_index < len(steps):
                        next_step = steps[next_index]
                        if next_step.descendant:
                            next_source = fresh_source(next_index)
                            direct_sources.append(next_source)
                            descendant_markers.append(
                                Marker(next_index, next_source)
                            )
                            active_context = True
                        else:
                            direct_sources.append(fresh_source(next_index))
                    else:
                        if step.descendant:
                            next_source = fresh_source(source.step_index)
                            direct_sources.append(next_source)
                            descendant_markers.append(
                                Marker(source.step_index, next_source)
                            )
                            active_context = True
                        matched_here = True
                        final_match = True

                stack.append(Frame(
                    elem, parent, direct_sources, descendant_markers,
                    enclosing_match, final_match, matched_here, active_context, []
                ))
                continue

            frame = stack.pop()
            if frame.element is not elem:
                raise ValueError('iterparse end event does not match the open element')

            buffered_match: Optional[tuple[Any, list[tuple[Any, list[Any]]]]] = None
            if frame.matched_here and id(elem) not in delivered:
                buffered_match = (elem, frame.pending)
            elif frame.active_context and frame.pending:
                if frame.enclosing_match is not None:
                    frame.enclosing_match.pending.extend(frame.pending)
                else:
                    for group in frame.pending:
                        cleaned = _clean_group(group, top_queue)
                        if cleaned is not None:
                            yield from _yield_group(cleaned, delivered)
            elif frame.pending and frame.enclosing_match is None:
                for group in frame.pending:
                    cleaned = _clean_group(group, top_queue)
                    if cleaned is not None:
                        yield from _yield_group(cleaned, delivered)

            if buffered_match is not None:
                if frame.enclosing_match is not None:
                    frame.enclosing_match.pending.append(buffered_match)
                elif frame.pending:
                    delivered.add(id(elem))
                    yield elem
                    for group in frame.pending:
                        cleaned = _clean_group(group, top_queue)
                        if cleaned is not None:
                            yield from _yield_group(cleaned, delivered)
                else:
                    delivered.add(id(elem))
                    yield elem

            if stack:
                parent_frame = stack[-1]
                inside_match = (
                    parent_frame.matched
                    or parent_frame.active_context
                    or parent_frame.enclosing_match is not None
                )
                if not inside_match and frame.parent is parent_frame.element:
                    children = parent_frame.element
                    if len(children) and children[-1] is elem:
                        del children[-1]
            elif not frame.matched:
                elem.clear()
    finally:
        close = getattr(events, 'close', None)
        if callable(close):
            close()


def _iterparse_select(source: Any,
                      steps: list[StreamStep],
                      absolute: bool,
                      events: Iterable[str] = ('start', 'end'),
                      iterparse_kwargs: Optional[dict[str, Any]] = None
                      ) -> Iterator[Any]:
    def event_source() -> Iterator[EventType]:
        parser_iterator = ElementTree.iterparse(
            source, events=events, **(iterparse_kwargs or {})
        )
        try:
            yield from parser_iterator
        finally:
            close = getattr(parser_iterator, 'close', None)
            if callable(close):
                close()

    yield from _iter_select_events_impl(event_source(), steps, absolute)


def iterparse_select(source: Any,
                     path: str,
                     namespaces: Optional[NamespacesType] = None,
                     parser: Optional[ParserClassType] = None,
                     events: Iterable[str] = ('start', 'end'),
                     **kwargs: Any) -> Iterator[Any]:
    """
    Parse *source* with ``ElementTree.iterparse`` while selecting matches.

    Unsupported XPath constructs are rejected before the XML parser is created.
    The parser iterator is created lazily, so consumer cancellation does not
    consume the rest of the document.
    """
    parser_class = parser or XPath2Parser
    iterparse_kwargs = kwargs.pop('iterparse_kwargs', None)
    absolute, steps = _compile_path(path, parser_class, namespaces, kwargs)
    return _iterparse_select(source, steps, absolute, events, iterparse_kwargs)
