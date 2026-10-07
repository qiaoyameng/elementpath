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
Streaming XPath selection on ElementTree iterparse-style events.

This module provides a selection entry point that does not require a fully
built document tree: it consumes ``(event, element)`` pairs as produced by
:func:`xml.etree.ElementTree.iterparse` (with ``events=('start', 'end')``)
and yields each selected element as soon as its subtree is complete.

Only a forward-only subset of XPath is supported, compiled and validated
when the selector is created:

* forward element paths: child (``/``) and descendant (``//``) steps with
  element name tests (plain names, prefixed names, ``*`` and ``prefix:*``
  wildcards), including the explicit ``child::`` and ``descendant::`` axes;
* attribute comparisons in predicates: ``[@a]``, ``[not(@a)]``,
  ``[@a='v']``, ``[@a!='v']`` and numeric comparisons, combined with
  ``and``/``or``;
* position conditions that are decidable online: ``[n]`` and comparisons
  of ``position()`` with an integer literal (``=``, ``!=``, ``<``, ``<=``,
  ``>``, ``>=``).

Expressions that depend on ``following``, ``preceding`` or other backward
or sibling axes, on an unbounded ``last()``, or on any other construct
outside the subset above are rejected at compile time raising
:class:`elementpath.exceptions.UnsupportedFeatureError`.

Matched subtrees are built and delivered whole; completed nodes that are
not selected are released as soon as their ``end`` event is processed.
Ancestor state that is still required by active matches is never cleared.
Results are produced in subtree-completion order: with nested matches an
inner match is therefore yielded before the enclosing one.
"""
import operator
from collections.abc import Iterable, Iterator
from typing import Any, Callable, Optional

from elementpath.aliases import NamespacesType, ParserClassType
from elementpath.exceptions import UnsupportedFeatureError
from elementpath.xpath2 import XPath2Parser

__all__ = ['StreamingSelector', 'iter_select_stream']

_COMPARISONS: dict = {
    '=': operator.eq, '!=': operator.ne, '<': operator.lt,
    '<=': operator.le, '>': operator.gt, '>=': operator.ge,
}
_REVERSED_COMPARISONS = {'<': '>', '<=': '>=', '>': '<', '>=': '<='}


def _unsupported(token: Any, part: str = 'expression') -> UnsupportedFeatureError:
    return UnsupportedFeatureError(
        f"{token!r} is not supported by streaming selection: only forward "
        f"element paths, attribute comparisons and online-decidable position "
        f"conditions are allowed in a streaming {part}"
    )


def _contains_symbol(token: Any, symbol: str) -> bool:
    if token.symbol == symbol:
        return True
    return any(_contains_symbol(t, symbol) for t in token)


class _NameTest:
    """An element name test for a streaming path step."""
    __slots__ = ('namespace', 'name')

    def __init__(self, namespace: Optional[str] = None,
                 name: Optional[str] = None) -> None:
        self.namespace = namespace
        self.name = name

    def match(self, tag: Any) -> bool:
        if not isinstance(tag, str):
            return False
        elif self.name is not None:
            if self.namespace:
                return tag == f'{{{self.namespace}}}{self.name}'
            return tag == self.name
        elif self.namespace:
            return tag.startswith(f'{{{self.namespace}}}')
        return True


class _Step:
    """A compiled step of a streaming path."""
    __slots__ = ('axis', 'name_test', 'predicates', 'positional_count')

    def __init__(self, axis: str, name_test: _NameTest,
                 predicates: list) -> None:
        self.axis = axis
        self.name_test = name_test
        self.predicates = predicates
        self.positional_count = sum(1 for positional, _ in predicates if positional)


def _resolve_name(value: str, namespaces: NamespacesType,
                  is_attribute: bool = False) -> _NameTest:
    if ':' in value:
        prefix, name = value.split(':')
        namespace = namespaces[prefix]
        return _NameTest(namespace, None if name == '*' else name)
    elif is_attribute or not namespaces.get(''):
        return _NameTest(None, value)
    return _NameTest(namespaces[''], value)


def _compile_name_test(token: Any, namespaces: NamespacesType) -> _NameTest:
    if token.symbol == '(name)':
        return _resolve_name(token.value, namespaces)
    elif token.symbol == '*':
        return _NameTest()
    elif token.symbol == ':':
        return _resolve_name(token.value, namespaces)
    raise _unsupported(token, 'step')


def _compile_attribute_name(token: Any, namespaces: NamespacesType) -> _NameTest:
    if token.symbol == '*':
        return _NameTest()
    elif token.symbol in ('(name)', ':'):
        return _resolve_name(token.value, namespaces, is_attribute=True)
    raise _unsupported(token, 'predicate')


def _attribute_key(name_test: _NameTest) -> str:
    if name_test.namespace:
        return f'{{{name_test.namespace}}}{name_test.name}'
    return name_test.name


def _make_attribute_comparison(name_test: _NameTest, symbol: str, value: Any) \
        -> Callable:
    if name_test.name is None:
        raise _unsupported(symbol, 'predicate')  # wildcard comparison unsupported
    name = _attribute_key(name_test)
    comparison = _COMPARISONS[symbol]

    if isinstance(value, str):
        def compare_string(elem: Any, position: Optional[int] = None) -> bool:
            attribute = elem.get(name)
            return attribute is not None and comparison(attribute, value)
        return compare_string

    def compare_number(elem: Any, position: Optional[int] = None) -> bool:
        attribute = elem.get(name)
        if attribute is None:
            return False
        try:
            return comparison(float(attribute), float(value))
        except ValueError:
            return False
    return compare_number


def _compile_attribute_test(token: Any, namespaces: NamespacesType) -> Callable:
    """Compiles a non-positional predicate based on attributes."""
    if token.symbol == '@':
        name_test = _compile_attribute_name(token[0], namespaces)

        def exists_test(elem: Any, position: Optional[int] = None) -> bool:
            if name_test.name is None:
                return bool(elem.attrib)
            return elem.get(_attribute_key(name_test)) is not None

        return exists_test

    elif token.symbol == 'not' and token[0].symbol == '@':
        name_test = _compile_attribute_name(token[0][0], namespaces)

        def not_exists_test(elem: Any, position: Optional[int] = None) -> bool:
            if name_test.name is None:
                return not elem.attrib
            return elem.get(_attribute_key(name_test)) is None

        return not_exists_test

    elif token.symbol in _COMPARISONS:
        left, right = token[0], token[1]
        if left.symbol == '@' and right.label == 'literal':
            name_test = _compile_attribute_name(left[0], namespaces)
            return _make_attribute_comparison(name_test, token.symbol, right.value)
        elif right.symbol == '@' and left.label == 'literal':
            name_test = _compile_attribute_name(right[0], namespaces)
            symbol = _REVERSED_COMPARISONS.get(token.symbol, token.symbol)
            return _make_attribute_comparison(name_test, symbol, left.value)
        elif _contains_symbol(token, 'last'):
            raise UnsupportedFeatureError(
                "unbounded last() is not supported by streaming selection"
            )
        raise _unsupported(token, 'predicate')

    elif token.symbol in ('and', 'or'):
        tests = [_compile_attribute_test(t, namespaces) for t in token]
        if token.symbol == 'and':
            def and_test(elem: Any, position: Optional[int] = None) -> bool:
                return all(test(elem) for test in tests)
            return and_test

        def or_test(elem: Any, position: Optional[int] = None) -> bool:
            return any(test(elem) for test in tests)
        return or_test

    elif token.symbol == 'last':
        raise UnsupportedFeatureError(
            "unbounded last() is not supported by streaming selection"
        )
    raise _unsupported(token, 'predicate')


def _compile_position_test(symbol: str, value: int, reversed_operands: bool) \
        -> Callable:
    if reversed_operands:
        symbol = _REVERSED_COMPARISONS.get(symbol, symbol)
    comparison = _COMPARISONS[symbol]

    def position_test(elem: Any, position: Optional[int] = None) -> bool:
        return position is not None and comparison(position, value)

    return position_test


def _compile_predicate(token: Any, namespaces: NamespacesType) -> tuple:
    """Compiles a predicate, returning a (positional, test) couple."""
    if token.symbol == '(integer)':
        return True, _compile_position_test('=', token.value, False)

    elif token.symbol in _COMPARISONS:
        left, right = token[0], token[1]
        if left.symbol == 'position' and right.symbol == '(integer)':
            return True, _compile_position_test(token.symbol, right.value, False)
        elif right.symbol == 'position' and left.symbol == '(integer)':
            return True, _compile_position_test(token.symbol, left.value, True)
        return False, _compile_attribute_test(token, namespaces)

    elif token.symbol == 'position':
        # position() alone means position() != 0, always true
        return True, _compile_position_test('!=', 0, False)

    return False, _compile_attribute_test(token, namespaces)


def _compile_step(token: Any, axis: str, namespaces: NamespacesType) -> _Step:
    predicates = []
    while token.symbol == '[':
        predicates.append(_compile_predicate(token[1], namespaces))
        token = token[0]
    predicates.reverse()

    if token.label == 'axis':
        if token.symbol not in ('child', 'descendant'):
            raise _unsupported(token, 'step')
        axis = token.symbol
        token = token[0]

    return _Step(axis, _compile_name_test(token, namespaces), predicates)


class StreamingSelector:
    """
    A compiled XPath selector for streaming selection over iterparse events.

    The *path* expression is validated at compile time against the supported
    streaming subset (see the module documentation): forward element paths,
    attribute comparisons and online-decidable position conditions. Anything
    else, including ``following``/``preceding`` axes and unbounded ``last()``
    usage, raises an :class:`elementpath.exceptions.UnsupportedFeatureError`.

    :param path: the XPath expression.
    :param namespaces: a dictionary with mapping from namespace prefixes into URIs.
    :param parser: the parser class to use, that is :class:`XPath2Parser` for default.
    :param kwargs: other optional parameters for the parser instance.
    """
    def __init__(self, path: str,
                 namespaces: Optional[NamespacesType] = None,
                 parser: Optional[ParserClassType] = None,
                 **kwargs: Any) -> None:
        if parser is None:
            parser = XPath2Parser
        if namespaces is None:
            namespaces = {}

        root_token = parser(namespaces, **kwargs).parse(path)
        self.absolute = False

        token = root_token
        steps: list = []
        while token.symbol in ('/', '//'):
            axis = 'child' if token.symbol == '/' else 'descendant'
            if len(token) == 1:
                self.absolute = True
                steps.append((axis, token[0]))
                break
            steps.append((axis, token[1]))
            token = token[0]
        else:
            steps.append(('child', token))

        if not steps:
            raise UnsupportedFeatureError(
                f"{path!r} is not supported by streaming selection"
            )
        self._steps = [_compile_step(t, axis, namespaces)
                       for axis, t in reversed(steps)]

    def iter_select(self, events: Iterable) -> Iterator:
        """
        Applies the compiled path to a stream of iterparse-style events.

        :param events: an iterable of ``(event, element)`` pairs as produced \
        by :func:`xml.etree.ElementTree.iterparse` using the ``start`` and \
        ``end`` events; other events are ignored.
        :return: an iterator over the selected elements. Each element is \
        yielded at its ``end`` event, when its subtree is complete.
        """
        steps = self._steps
        last_step = len(steps) - 1
        open_matches = 0
        # Stack entries: [element, matched, armed], where *armed* maps a
        # step index to the counters of its positional predicates.
        stack: list = [[None, False, {}]]
        if self.absolute:
            stack[0][2][0] = [0] * steps[0].positional_count

        for event, elem in events:
            if event == 'start':
                parent = stack[-1]
                matched_steps = []
                for step_index, counters in parent[2].items():
                    step = steps[step_index]
                    if not step.name_test.match(elem.tag):
                        continue
                    k = 0
                    for positional, test in step.predicates:
                        if positional:
                            counters[k] += 1
                            position = counters[k]
                            k += 1
                        else:
                            position = None
                        if not test(elem, position):
                            break
                    else:
                        matched_steps.append(step_index)

                entry = [elem, False, {}]
                armed = entry[2]
                for step_index in parent[2]:
                    if steps[step_index].axis == 'descendant':
                        armed[step_index] = \
                            [0] * steps[step_index].positional_count
                for step_index in matched_steps:
                    if step_index == last_step:
                        entry[1] = True
                        open_matches += 1
                    elif step_index + 1 not in armed:
                        next_step = steps[step_index + 1]
                        armed[step_index + 1] = \
                            [0] * next_step.positional_count

                if not self.absolute and len(stack) == 1 and 0 not in armed:
                    # The root element is the context item of relative paths.
                    armed[0] = [0] * steps[0].positional_count
                stack.append(entry)

            elif event == 'end':
                entry = stack.pop()
                if entry[1]:
                    open_matches -= 1
                    yield elem
                    if not open_matches and len(stack) > 1:
                        stack[-1][0].remove(elem)
                elif not open_matches:
                    # Completed node that is not selected and is not part of
                    # an active match: release it, keeping the ancestor state
                    # required by active matches untouched.
                    elem.clear()
                    if len(stack) > 1:
                        stack[-1][0].remove(elem)


def iter_select_stream(events: Iterable,
                       path: str,
                       namespaces: Optional[NamespacesType] = None,
                       parser: Optional[ParserClassType] = None,
                       **kwargs: Any) -> Iterator:
    """
    A function that creates a streaming XPath selector generator that applies
    a *path* expression to a stream of ElementTree iterparse-style *events*,
    without building the full document tree.

    Only a forward-only XPath subset is supported (forward element paths,
    attribute comparisons and online-decidable position conditions); other
    expressions are rejected at compile time. See :class:`StreamingSelector`
    for the accepted arguments.

    :param events: an iterable of ``(event, element)`` pairs as produced by \
    :func:`xml.etree.ElementTree.iterparse` using the ``start`` and ``end`` \
    events; other events are ignored.
    :param path: the XPath expression.
    :return: an iterator over the selected elements, each yielded at its \
    ``end`` event, when its subtree is complete.
    """
    return StreamingSelector(path, namespaces, parser, **kwargs).iter_select(events)
