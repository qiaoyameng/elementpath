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
Static type analysis for XPath expressions.

This module provides an optional static checker that infers the sequence type
of an XPath expression without evaluating it, in order to detect type errors
(like passing a node sequence to a function that accepts only numbers) before
the expression is saved or executed.

The analysis propagates static types (item type and occurrence) along the token
tree produced by the parser, using the static context of the parser instance
(in-scope variable types, registered function signatures and statically known
documents) plus an optional context item type. When a subexpression cannot be
statically typed its type is *unknown*: unknown types never generate errors,
because a problem is reported only when it can be proved for every possible
runtime value. Functions are never executed to guess their result types, so
the analysis has no side effects (it doesn't consume variable values and
doesn't load external documents).

Supported constructs in this version:

- literals, variable references, context item and parenthesis expressions;
- path expressions ('/', '//'), path steps (names, wildcards, kind tests,
  axes, '@', '..') and predicates;
- arithmetic operators ('+', '-', '*', 'div', 'idiv', 'mod') with XSD numeric
  promotion (xs:integer -> xs:decimal -> xs:float -> xs:double);
- value and general comparisons and the 'to' operator;
- sequence concatenation (',') and set operators ('union', '|', 'intersect',
  'except');
- conditional expressions ('if'), with merging of branch types;
- 'for', 'let', 'some' and 'every' expressions, with static typing of the
  bound variables;
- type expressions ('instance of', 'treat as', 'cast as', 'castable as'),
  including a check of impossible casts between XSD type families;
- calls to functions and constructors that declare a signature with sequence
  types (all the builtin functions and the external functions registered with
  *sequence_types*), checked with the function conversion rules (atomization
  and type promotions); the *fn:doc* result is refined using the statically
  known documents of the parser.

Other constructs (e.g. maps, arrays, arrow and lookup operators, dynamic
function calls) are traversed for finding errors in their subexpressions but
their result type is unknown.
"""
from urllib.parse import urljoin, urlparse

import elementpath.aliases as ta

from elementpath.datatypes import builtin_atomic_types, AnyAtomicType, AnySimpleType, \
    NumericProxy, DoubleProxy, DecimalProxy, Float, Integer, StringProxy, BooleanProxy, \
    AbstractQName, AnyURI, AbstractBinary, AbstractDateTime, Duration, UntypedAtomic
from elementpath.exceptions import ElementPathError
from elementpath.namespaces import get_expanded_name
from elementpath.sequence_types import is_sequence_type_restriction, \
    normalize_sequence_type
from elementpath.tdop import MultiLabel
from elementpath.xpath_tokens import XPathToken, XPathFunction

__all__ = ['StaticType', 'StaticAnalysisResult', 'ExpressionAnalyzer',
           'analyze_expression']

OccurrenceType = tuple[int | None, int | None]
"""Occurrence range of a static type: (min, max), None max means unbounded."""

###
# Occurrence ranges: couples (min, max) where max=None means unbounded;
# the occurrence is unknown when min is None.
OCCURS_EMPTY: OccurrenceType = (0, 0)
OCCURS_ONE: OccurrenceType = (1, 1)
OCCURS_OPTIONAL: OccurrenceType = (0, 1)
OCCURS_ONE_OR_MORE: OccurrenceType = (1, None)
OCCURS_ZERO_OR_MORE: OccurrenceType = (0, None)
OCCURS_UNKNOWN: OccurrenceType = (None, None)

_OCCURRENCE_INDICATORS: dict[OccurrenceType, str] = {
    OCCURS_ONE: '',
    OCCURS_OPTIONAL: '?',
    OCCURS_ONE_OR_MORE: '+',
    OCCURS_ZERO_OR_MORE: '*',
}

_LITERAL_TYPES = {
    '(integer)': 'xs:integer',
    '(decimal)': 'xs:decimal',
    '(float)': 'xs:double',
    '(string)': 'xs:string',
}

_NODE_TYPE_PREFIXES = ('element(', 'attribute(', 'text(', 'comment(',
                       'document-node(', 'processing-instruction(', 'namespace(')


def _is_node_type(item_type: str) -> bool:
    return item_type == 'node()' or item_type.startswith(_NODE_TYPE_PREFIXES)


def _node_kind(item_type: str) -> str:
    return item_type.partition('(')[0]


def _atomic_class(item_type: str) -> type | None:
    """Returns the XSD builtin class of an atomic item type, `None` otherwise."""
    return builtin_atomic_types.get(item_type)


def _class_issubclass(cls: type, base: type) -> bool:
    """
    An issubclass() variant that works also with XSD proxy classes, whose
    __subclasshook__ vetoes plain subclass checks (e.g. issubclass(X, X)).
    """
    return base in cls.__mro__ or issubclass(cls, base)


def _is_numeric_class(cls: type) -> bool:
    return issubclass(cls, NumericProxy) or \
        any(base in cls.__mro__ for base in (DecimalProxy, DoubleProxy))


def _is_numeric_type(item_type: str) -> bool:
    if item_type in ('xs:numeric', 'numeric'):
        return True
    cls = _atomic_class(item_type)
    return cls is not None and _is_numeric_class(cls)


def _lub_item_types(st1: str, st2: str) -> str:
    """Returns the least upper bound of two item types."""
    if st1 == st2:
        return st1
    elif is_sequence_type_restriction(st1, st2):
        return st1
    elif is_sequence_type_restriction(st2, st1):
        return st2
    elif _is_numeric_type(st1) and _is_numeric_type(st2):
        return 'xs:numeric'
    elif _is_node_type(st1) and _is_node_type(st2):
        return 'node()'
    elif _atomic_class(st1) is not None and _atomic_class(st2) is not None:
        return 'xs:anyAtomicType'
    else:
        return 'item()'


def _union_occurrence(occ1: OccurrenceType, occ2: OccurrenceType) -> OccurrenceType:
    if occ1[0] is None or occ2[0] is None:
        return OCCURS_UNKNOWN
    max_occurs = None if occ1[1] is None or occ2[1] is None else max(occ1[1], occ2[1])
    return min(occ1[0], occ2[0]), max_occurs


def _concat_occurrence(occ1: OccurrenceType, occ2: OccurrenceType) -> OccurrenceType:
    if occ1[0] is None or occ2[0] is None:
        return OCCURS_UNKNOWN
    max_occurs = None if occ1[1] is None or occ2[1] is None else occ1[1] + occ2[1]
    return occ1[0] + occ2[0], max_occurs


class StaticType:
    """
    The static type of an XPath expression, defined by an item type and an
    occurrence range. The type is *unknown* when the item type is `None`:
    unknown types are compatible with every check by definition.
    """
    __slots__ = ('item_type', 'min_occurs', 'max_occurs')

    item_type: str | None
    min_occurs: int | None
    max_occurs: int | None

    def __init__(self, item_type: str | None = None,
                 occurrence: OccurrenceType = OCCURS_UNKNOWN) -> None:
        self.item_type = item_type
        self.min_occurs, self.max_occurs = occurrence

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}({self})'

    def __str__(self) -> str:
        return self.to_sequence_type() or 'unknown'

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StaticType) and \
            self.item_type == other.item_type and \
            self.min_occurs == other.min_occurs and \
            self.max_occurs == other.max_occurs

    @property
    def is_unknown(self) -> bool:
        return self.item_type is None

    @property
    def occurrence(self) -> OccurrenceType:
        if self.min_occurs is None:
            return OCCURS_UNKNOWN
        return self.min_occurs, self.max_occurs

    @classmethod
    def unknown(cls) -> 'StaticType':
        return cls()

    @classmethod
    def empty_sequence(cls) -> 'StaticType':
        return cls('empty-sequence()', OCCURS_EMPTY)

    @classmethod
    def from_sequence_type(cls, sequence_type: str | None) -> 'StaticType':
        """Builds a static type from a sequence type string."""
        if not sequence_type:
            return cls.unknown()

        st = normalize_sequence_type(sequence_type)
        occurrence = OCCURS_ONE
        if st[-1] in '?*+':
            occurrence = {'?': OCCURS_OPTIONAL, '*': OCCURS_ZERO_OR_MORE,
                          '+': OCCURS_ONE_OR_MORE}[st[-1]]
            st = st[:-1]

        if st in ('empty-sequence()', 'none'):
            return cls.empty_sequence()
        elif st == 'numeric':
            st = 'xs:numeric'
        return cls(st, occurrence)

    def to_sequence_type(self) -> str | None:
        """Returns the sequence type string, `None` if the type is unknown."""
        if self.item_type is None:
            return None
        elif self.item_type == 'empty-sequence()':
            return 'empty-sequence()'
        try:
            indicator = _OCCURRENCE_INDICATORS[self.occurrence]
        except KeyError:
            indicator = '+' if self.min_occurs else '*'
        return f'{self.item_type}{indicator}'

    def merge(self, other: 'StaticType') -> 'StaticType':
        """
        Returns the union of two static types, that is the static type of an
        expression that can evaluate to one of them (e.g. the merge of the
        branches of a conditional expression).
        """
        if self.item_type is None or other.item_type is None:
            return self.unknown()
        elif self.item_type == 'empty-sequence()':
            item_type = other.item_type
        elif other.item_type == 'empty-sequence()':
            item_type = self.item_type
        else:
            item_type = _lub_item_types(self.item_type, other.item_type)
        return StaticType(item_type, _union_occurrence(self.occurrence, other.occurrence))

    def concat(self, other: 'StaticType') -> 'StaticType':
        """Returns the static type of the concatenation of two sequences."""
        if self.item_type is None or other.item_type is None:
            return self.unknown()
        elif self.item_type == 'empty-sequence()':
            item_type = other.item_type
        elif other.item_type == 'empty-sequence()':
            item_type = self.item_type
        else:
            item_type = _lub_item_types(self.item_type, other.item_type)
        return StaticType(item_type, _concat_occurrence(self.occurrence, other.occurrence))

    def widened(self) -> 'StaticType':
        """
        Returns the type with a zero-or-more occurrence, that is the static
        type of an expression evaluated once for each item of a sequence
        (path steps and for-clause results).
        """
        if self.is_unknown:
            return self.unknown()
        return StaticType(self.item_type, OCCURS_ZERO_OR_MORE)


def _numeric_rank(item_type: str) -> int | None:
    """
    Returns the numeric promotion rank of an item type
    (0=xs:integer, 1=xs:decimal, 2=xs:float, 3=xs:double) or `None`.
    """
    cls = _atomic_class(item_type)
    if cls is None:
        return None
    mro = cls.__mro__
    if UntypedAtomic in mro:
        return 3  # xs:untypedAtomic is cast to xs:double
    elif Integer in mro:
        return 0
    elif DecimalProxy in mro:
        return 1
    elif Float in mro:
        return 2
    elif DoubleProxy in mro:
        return 3
    return None


_NUMERIC_RESULT_TYPES = ('xs:integer', 'xs:decimal', 'xs:float', 'xs:double')

# Atomic type families for which no arithmetic operator is defined
_NOT_ARITHMETIC_TYPES = (StringProxy, BooleanProxy, AbstractQName, AnyURI, AbstractBinary)


def _cast_family(cls: type) -> str | None:
    """Returns the XSD casting family of an atomic type class."""
    if _is_numeric_class(cls):
        return 'numeric'
    elif _class_issubclass(cls, StringProxy):
        return 'string'
    elif _class_issubclass(cls, BooleanProxy):
        return 'boolean'
    elif _class_issubclass(cls, AbstractQName):
        return 'qname'
    elif _class_issubclass(cls, AnyURI):
        return 'uri'
    elif _class_issubclass(cls, AbstractBinary):
        return 'binary'
    elif _class_issubclass(cls, AbstractDateTime):
        return 'datetime'
    elif _class_issubclass(cls, Duration):
        return 'duration'
    return None


def _is_castable(source_item: str, target_name: str) -> bool:
    """
    Returns `False` only if a cast from the source item type to the target
    atomic type is not permitted for any value of the source type.
    """
    if _is_node_type(source_item):
        return True  # nodes are atomized to xs:untypedAtomic

    source_cls = _atomic_class(source_item)
    target_cls = _atomic_class(target_name)
    if source_cls is None or target_cls is None:
        return True  # not decidable (e.g. schema-defined types)
    elif source_cls in (AnyAtomicType, AnySimpleType) or \
            _class_issubclass(source_cls, UntypedAtomic) or \
            _class_issubclass(source_cls, StringProxy):
        return True
    elif target_cls in (AnyAtomicType, AnySimpleType) or \
            _class_issubclass(target_cls, UntypedAtomic) or \
            _class_issubclass(target_cls, StringProxy):
        return True

    source_family = _cast_family(source_cls)
    target_family = _cast_family(target_cls)
    if source_family is None or target_family is None:
        return True
    elif source_family == target_family:
        return True
    return {source_family, target_family} == {'string', 'uri'}


def _match_function_conversion(actual_item: str, expected_item: str) -> bool:
    """
    Returns `False` only if an actual item type can never match an expected
    item type applying the function conversion rules (subtype substitution,
    atomization, xs:untypedAtomic casting and numeric/URI promotions).
    """
    if expected_item in ('item()', 'xs:anyType', 'xs:anySimpleType', 'xs:anyAtomicType'):
        return True
    elif actual_item in ('item()', 'xs:untypedAtomic',
                         'xs:anyAtomicType', 'xs:anySimpleType'):
        return True
    elif is_sequence_type_restriction(expected_item, actual_item):
        return True

    if _is_node_type(actual_item):
        if _is_node_type(expected_item):
            # Different node kinds are incompatible (e.g. attribute() vs element())
            return _node_kind(actual_item) == _node_kind(expected_item)
        # Node atomization produces xs:untypedAtomic values with schema-less
        # documents, that are castable to any atomic type: runtime-dependent.
        return True
    elif _is_node_type(expected_item):
        return False  # atomization never produces nodes

    if expected_item == 'xs:numeric':
        return _is_numeric_type(actual_item)
    elif expected_item.startswith(('function(', 'map(', 'array(')):
        return True  # function items are not checked in this version

    expected_cls = _atomic_class(expected_item)
    actual_cls = _atomic_class(actual_item)
    if expected_cls is None or actual_cls is None:
        return True  # not decidable (e.g. schema-defined types)

    if expected_item in ('xs:double', 'xs:float'):
        # Numeric promotion: xs:decimal -> xs:float -> xs:double
        return _is_numeric_type(actual_item)
    elif _is_numeric_type(expected_item):
        # Other numeric target types admit only subtype substitution
        return False
    elif _class_issubclass(expected_cls, StringProxy):
        # xs:anyURI is promotable to xs:string
        return _class_issubclass(actual_cls, StringProxy) or \
            _class_issubclass(actual_cls, AnyURI)
    return False


class ExpressionAnalyzer:
    """
    Static analyzer of XPath expressions. It walks the token tree produced by
    a parser and infers the static type of each subexpression, collecting the
    provable type errors.

    :param parser: the parser instance that defines the static context.
    :param context_item_type: the sequence type of the context item, \
    'item()' for default.
    :param variable_types: an optional mapping of in-scope variable types \
    that extends the variable types of the parser's static context.
    """

    def __init__(self, parser: ta.XPathParserType, context_item_type: str = 'item()',
                 variable_types: dict[str, str] | None = None) -> None:
        self.parser = parser
        self.errors: list[ElementPathError] = []
        self.context_item_type = StaticType.from_sequence_type(context_item_type)

        self.variables: dict[str, StaticType] = {}
        types = dict(getattr(parser, 'variable_types', None) or {})
        if variable_types:
            types.update(variable_types)
        for varname, sequence_type in types.items():
            self.variables[varname] = StaticType.from_sequence_type(sequence_type)

    def _add_error(self, token: XPathToken, code: str, message: str) -> None:
        self.errors.append(token.error(code, message))

    def analyze(self, token: XPathToken) -> StaticType:
        """Returns the static type of the expression rooted at a token."""
        symbol = token.symbol

        if symbol in _LITERAL_TYPES:
            return StaticType(_LITERAL_TYPES[symbol], OCCURS_ONE)
        elif symbol == '$':
            return self._analyze_variable(token)
        elif symbol == '.':
            return self.context_item_type
        elif symbol == '..':
            return StaticType('node()', OCCURS_OPTIONAL)
        elif symbol == '(':
            return self._analyze_parenthesized(token)
        elif symbol == ',':
            return self._analyze_comma(token)
        elif symbol == 'if':
            return self._analyze_if(token)
        elif symbol in ('for', 'let'):
            return self._analyze_for(token)
        elif symbol in ('some', 'every'):
            return self._analyze_quantified(token)
        elif symbol in ('+', '-', 'div', 'idiv', 'mod') or \
                symbol == '*' and len(token) == 2:
            return self._analyze_arithmetic(token)
        elif symbol == '*':
            return StaticType('element()', OCCURS_ZERO_OR_MORE)
        elif symbol in ('eq', 'ne', 'lt', 'gt', 'le', 'ge'):
            return self._analyze_value_comparison(token)
        elif symbol in ('=', '!=', '<', '>', '<=', '>='):
            return self._analyze_general_comparison(token)
        elif symbol == 'to':
            return self._analyze_to(token)
        elif symbol in ('union', '|', 'intersect', 'except'):
            return self._analyze_set_operation(token)
        elif symbol == 'instance':
            self.analyze(token[0])
            return StaticType('xs:boolean', OCCURS_ONE)
        elif symbol == 'treat':
            return self._analyze_treat(token)
        elif symbol in ('cast', 'castable'):
            return self._analyze_cast(token)
        elif symbol in ('/', '//'):
            return self._analyze_path(token)
        elif symbol == '[':
            return self._analyze_predicate(token)
        elif symbol == '@':
            return StaticType('attribute()', OCCURS_ZERO_OR_MORE)
        elif symbol in ('(name)', ':', 'Q{'):
            return self._analyze_name_step(token)
        elif symbol == '||':
            self.analyze(token[0])
            self.analyze(token[1])
            return StaticType('xs:string', OCCURS_ONE)
        elif symbol == '!':
            self.analyze(token[0])
            return self.analyze(token[1]).widened()
        elif isinstance(token, XPathFunction):
            return self._analyze_function(token)
        elif token.label == 'axis':
            return self._analyze_axis(token)
        else:
            return self._analyze_generic(token)

    def _analyze_generic(self, token: XPathToken) -> StaticType:
        for child in token:
            self.analyze(child)
        return StaticType.unknown()

    def _analyze_variable(self, token: XPathToken) -> StaticType:
        varname = token.value
        try:
            return self.variables[varname]
        except KeyError:
            pass

        if not varname.startswith('{'):
            try:
                expanded_name = get_expanded_name(varname, self.parser.namespaces)
            except KeyError:
                pass
            else:
                try:
                    return self.variables[expanded_name]
                except KeyError:
                    pass
        return StaticType.unknown()

    def _analyze_parenthesized(self, token: XPathToken) -> StaticType:
        if not token:
            return StaticType.empty_sequence()
        elif len(token) == 1:
            return self.analyze(token[0])
        else:
            # Dynamic function call or partial application
            return self._analyze_generic(token)

    def _analyze_comma(self, token: XPathToken) -> StaticType:
        result = self.analyze(token[0])
        for child in token[1:]:
            result = result.concat(self.analyze(child))
        return result

    def _analyze_if(self, token: XPathToken) -> StaticType:
        condition = self.analyze(token[0])
        self._check_effective_boolean_value(condition, token[0])
        return self.analyze(token[1]).merge(self.analyze(token[2]))

    def _analyze_for(self, token: XPathToken) -> StaticType:
        saved = self._bind_variables(token, single_item=token.symbol == 'for')
        try:
            result = self.analyze(token[-1])
        finally:
            self._restore_variables(saved)

        if token.symbol == 'for':
            return result.widened()
        return result

    def _analyze_quantified(self, token: XPathToken) -> StaticType:
        saved = self._bind_variables(token, single_item=True)
        try:
            satisfies = self.analyze(token[-1])
            self._check_effective_boolean_value(satisfies, token[-1])
        finally:
            self._restore_variables(saved)
        return StaticType('xs:boolean', OCCURS_ONE)

    def _bind_variables(self, token: XPathToken,
                        single_item: bool) -> dict[str, StaticType | None]:
        saved: dict[str, StaticType | None] = {}
        for k in range(0, len(token) - 1, 2):
            var_token, range_token = token[k], token[k + 1]
            range_type = self.analyze(range_token)
            varname = var_token.value
            if varname not in saved:
                saved[varname] = self.variables.get(varname)
            if single_item:
                # Each iteration binds the variable to a single item
                self.variables[varname] = StaticType(range_type.item_type, OCCURS_ONE)
            else:
                self.variables[varname] = range_type
        return saved

    def _restore_variables(self, saved: dict[str, StaticType | None]) -> None:
        for varname, static_type in saved.items():
            if static_type is None:
                del self.variables[varname]
            else:
                self.variables[varname] = static_type

    def _analyze_arithmetic(self, token: XPathToken) -> StaticType:
        if len(token) == 1:
            # Unary plus/minus operator
            operand = self.analyze(token[0])
            self._check_arithmetic_operand(operand, token)
            if operand.item_type is None:
                return StaticType.unknown()
            rank = _numeric_rank(operand.item_type)
            if rank is None:
                return StaticType(None, operand.occurrence)
            return StaticType(_NUMERIC_RESULT_TYPES[rank], operand.occurrence)

        left = self.analyze(token[0])
        right = self.analyze(token[1])
        self._check_arithmetic_operand(left, token)
        self._check_arithmetic_operand(right, token)

        occurrence = self._binary_occurrence(left, right)
        if left.item_type is None or right.item_type is None:
            return StaticType(None, occurrence)

        rank1 = _numeric_rank(left.item_type)
        rank2 = _numeric_rank(right.item_type)
        if _is_node_type(left.item_type) or _is_node_type(right.item_type):
            item_type = None  # atomization result is runtime-dependent
        elif rank1 is None or rank2 is None:
            others = [t for t, r in ((left.item_type, rank1), (right.item_type, rank2))
                      if r is None]
            if all(t in ('xs:numeric', 'numeric') for t in others):
                item_type = 'xs:numeric'
            else:
                item_type = None  # e.g. xs:anyAtomicType or date/duration arithmetic
        elif token.symbol == 'idiv':
            item_type = 'xs:integer'
        elif token.symbol == 'div':
            rank = max(rank1, rank2)
            item_type = 'xs:double' if rank == 3 else \
                'xs:float' if rank == 2 else 'xs:decimal'
        else:
            item_type = _NUMERIC_RESULT_TYPES[max(rank1, rank2)]

        return StaticType(item_type, occurrence)

    @staticmethod
    def _binary_occurrence(left: StaticType, right: StaticType) -> OccurrenceType:
        if left.min_occurs is None or right.min_occurs is None:
            return OCCURS_UNKNOWN
        return (0 if 0 in (left.min_occurs, right.min_occurs) else 1), 1

    def _check_arithmetic_operand(self, static_type: StaticType,
                                  token: XPathToken) -> None:
        item_type = static_type.item_type
        if item_type is None or item_type == 'empty-sequence()':
            return
        elif static_type.min_occurs is not None and static_type.min_occurs >= 2:
            self._add_error(token, 'XPTY0004',
                            'arithmetic operator is not defined on a sequence '
                            'of two or more items')
        elif _is_node_type(item_type) or item_type == 'item()':
            return  # atomization or undetermined type: runtime-dependent
        else:
            cls = _atomic_class(item_type)
            if cls is not None and any(_class_issubclass(cls, base)
                                       for base in _NOT_ARITHMETIC_TYPES):
                self._add_error(token, 'XPTY0004',
                                'arithmetic operator is not defined for an '
                                f'operand of type {item_type!r}')

    def _analyze_value_comparison(self, token: XPathToken) -> StaticType:
        left = self.analyze(token[0])
        right = self.analyze(token[1])
        for static_type in (left, right):
            if static_type.min_occurs is not None and static_type.min_occurs >= 2:
                self._add_error(token, 'XPTY0004',
                                'value comparison cannot be applied to a sequence '
                                'of two or more items')
        return StaticType('xs:boolean', self._binary_occurrence(left, right))

    def _analyze_general_comparison(self, token: XPathToken) -> StaticType:
        self.analyze(token[0])
        self.analyze(token[1])
        return StaticType('xs:boolean', OCCURS_ONE)

    def _analyze_to(self, token: XPathToken) -> StaticType:
        for child in token:
            static_type = self.analyze(child)
            item_type = static_type.item_type
            if item_type is None or item_type == 'item()' or _is_node_type(item_type):
                continue
            cls = _atomic_class(item_type)
            if cls is not None and not any(_class_issubclass(cls, base) for base in
                                           (Integer, UntypedAtomic,
                                            AnyAtomicType, AnySimpleType)):
                self._add_error(token, 'XPTY0004',
                                f"operand of 'to' is not an integer: {item_type!r}")
        return StaticType('xs:integer', OCCURS_ZERO_OR_MORE)

    def _analyze_set_operation(self, token: XPathToken) -> StaticType:
        for child in token:
            static_type = self.analyze(child)
            item_type = static_type.item_type
            if item_type is not None and item_type not in ('item()', 'empty-sequence()') \
                    and not _is_node_type(item_type):
                self._add_error(token, 'XPTY0004',
                                f'operand of {token.symbol!r} is not a node '
                                f'sequence: {item_type!r}')
        return StaticType('node()', OCCURS_ZERO_OR_MORE)

    def _analyze_treat(self, token: XPathToken) -> StaticType:
        self.analyze(token[0])
        type_token = token[1]
        occurrence = getattr(type_token, 'occurrence', '') or ''
        if type_token.symbol == 'empty-sequence':
            sequence_type = 'empty-sequence()'
        elif type_token.label == 'kind test':
            sequence_type = f'{type_token.symbol}(){occurrence}'
        else:
            sequence_type = f'{type_token.source}{occurrence}'
        return StaticType.from_sequence_type(sequence_type)

    def _analyze_cast(self, token: XPathToken) -> StaticType:
        source_type = self.analyze(token[0])
        type_token = token[1]
        type_name = type_token.source.rstrip('*+?')
        optional = getattr(type_token, 'occurrence', '') == '?'

        if token.symbol == 'castable':
            return StaticType('xs:boolean', OCCURS_ONE)

        if source_type.min_occurs is not None and source_type.min_occurs >= 2:
            self._add_error(token, 'XPTY0004',
                            'more than one value in expression for cast operator')
        elif source_type.item_type == 'empty-sequence()' and not optional:
            self._add_error(token, 'XPTY0004',
                            'an atomic value is required for cast operator')
        elif source_type.item_type is not None and \
                not _is_castable(source_type.item_type, type_name):
            self._add_error(token, 'XPTY0004',
                            f'values of type {source_type.item_type!r} cannot '
                            f'be cast to {type_name!r}')

        if optional or source_type.min_occurs == 0:
            occurrence = OCCURS_OPTIONAL
        elif source_type.min_occurs is None:
            occurrence = OCCURS_UNKNOWN
        else:
            occurrence = OCCURS_ONE
        return StaticType(type_name, occurrence)

    def _analyze_path(self, token: XPathToken) -> StaticType:
        if not token:
            return StaticType('document-node()', OCCURS_OPTIONAL)
        elif len(token) == 1:
            right = self.analyze(token[0])
        else:
            left = self.analyze(token[0])
            item_type = left.item_type
            if item_type is not None and item_type not in ('item()', 'empty-sequence()') \
                    and not _is_node_type(item_type):
                self._add_error(token, 'XPTY0019',
                                'left operand of a path operator must be a node '
                                f'sequence, not {item_type!r}')
            right = self.analyze(token[1])
        return right.widened()

    def _analyze_predicate(self, token: XPathToken) -> StaticType:
        left = self.analyze(token[0])
        self.analyze(token[1])
        if left.is_unknown:
            return left
        elif left.min_occurs is None:
            return StaticType(left.item_type, OCCURS_UNKNOWN)
        return StaticType(left.item_type, (0, left.max_occurs))

    def _analyze_name_step(self, token: XPathToken) -> StaticType:
        if token.symbol == ':' and len(token) == 2 and isinstance(token[1], XPathFunction):
            # A prefixed function call (e.g. fn:abs(...))
            return self._analyze_function(token[1])
        # A name test used as a path step selects child elements
        return StaticType('element()', OCCURS_ZERO_OR_MORE)

    def _analyze_axis(self, token: XPathToken) -> StaticType:
        for child in token:
            self.analyze(child)
        if token.symbol == 'attribute':
            return StaticType('attribute()', OCCURS_ZERO_OR_MORE)
        elif token.symbol in ('self', 'parent'):
            return StaticType('node()', OCCURS_OPTIONAL)
        return StaticType('element()', OCCURS_ZERO_OR_MORE)

    def _analyze_function(self, token: XPathFunction) -> StaticType:
        label = token.label
        if label == 'kind test' or \
                isinstance(label, MultiLabel) and 'kind test' in label:
            # A kind test used as a path step (e.g. node(), text())
            return StaticType(f'{token.symbol}()', OCCURS_ZERO_OR_MORE)

        arg_types = []
        for arg in token:
            if arg.symbol == '?':
                # Partial function application: the result is a function item
                return self._analyze_generic(token)
            arg_types.append(self.analyze(arg))

        sequence_types = token.sequence_types
        if not sequence_types or isinstance(sequence_types, str):
            # No signature available (e.g. an external function registered
            # without sequence types): the function is not executed to
            # guess its result type.
            return StaticType.unknown()

        params, return_type = sequence_types[:-1], sequence_types[-1]
        for index, (arg_type, param_type) in enumerate(zip(arg_types, params)):
            self.check_argument(arg_type, param_type, token, index + 1)

        result = StaticType.from_sequence_type(return_type)
        if token.symbol == 'doc':
            result = self._refine_document_type(token, result)
        return result

    def _refine_document_type(self, token: XPathFunction,
                              result: StaticType) -> StaticType:
        document_types = getattr(self.parser, 'document_types', None)
        if not document_types or not token or token[0].symbol != '(string)':
            return result

        uri = token[0].value.strip()
        base_uri = getattr(self.parser, 'base_uri', None)
        if base_uri and not urlparse(uri).scheme:
            uri = urljoin(base_uri, uri)
        try:
            return StaticType.from_sequence_type(document_types[uri])
        except KeyError:
            return result

    def check_argument(self, actual: StaticType, expected_type: str,
                       token: XPathToken, position: int) -> None:
        """
        Checks an argument static type against the sequence type of a function
        parameter, adding an error to the analysis if they are incompatible.
        """
        expected = StaticType.from_sequence_type(expected_type)
        if actual.item_type is None or expected.item_type is None:
            return
        elif expected.item_type == 'empty-sequence()':
            if actual.min_occurs is not None and actual.min_occurs > 0:
                self._add_error(token, 'XPTY0004',
                                f'argument {position} must be an empty sequence')
            return

        if actual.min_occurs is not None and expected.max_occurs is not None \
                and actual.min_occurs > expected.max_occurs:
            self._add_error(
                token, 'XPTY0004',
                f'argument {position} of type {actual} has too many items '
                f'for the required type {expected}'
            )
        elif actual.max_occurs is not None and expected.min_occurs is not None and \
                actual.max_occurs < expected.min_occurs:
            self._add_error(
                token, 'XPTY0004',
                f'argument {position} of type {actual} has too few items '
                f'for the required type {expected}'
            )
        elif not _match_function_conversion(actual.item_type, expected.item_type):
            self._add_error(
                token, 'XPTY0004',
                f'argument {position} of type {actual} is not appropriate '
                f'for the required type {expected}'
            )

    def _check_effective_boolean_value(self, static_type: StaticType,
                                       token: XPathToken) -> None:
        item_type = static_type.item_type
        if item_type is None or item_type in ('item()', 'empty-sequence()') \
                or _is_node_type(item_type):
            return

        cls = _atomic_class(item_type)
        if cls is None:
            return
        elif static_type.min_occurs is not None and static_type.min_occurs >= 2:
            self._add_error(token, 'FORG0006',
                            'effective boolean value is not defined for a '
                            'sequence of two or more atomic values')
        elif not _is_numeric_class(cls) and not any(
                _class_issubclass(cls, base) for base in
                (BooleanProxy, StringProxy, UntypedAtomic, AnyAtomicType, AnySimpleType)):
            self._add_error(token, 'FORG0006',
                            'effective boolean value is not defined for '
                            f'type {item_type!r}')


class StaticAnalysisResult:
    """
    The result of the static analysis of an XPath expression.

    :param expression_type: the inferred static type of the expression.
    :param errors: the list of the provable static type errors found by \
    the analysis. Each error carries the token it refers to, so the \
    position in the source expression is available with `error.token.position`.
    """

    def __init__(self, expression_type: StaticType,
                 errors: list[ElementPathError]) -> None:
        self.expression_type = expression_type
        self.errors = errors

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}(type={self}, errors={len(self.errors)})'

    def __bool__(self) -> bool:
        return self.ok

    @property
    def ok(self) -> bool:
        """`True` if no provable static type error has been found."""
        return not self.errors

    @property
    def unknown(self) -> bool:
        """`True` if the expression type cannot be statically determined."""
        return self.expression_type.is_unknown

    @property
    def sequence_type(self) -> str | None:
        """The inferred sequence type of the expression, `None` if unknown."""
        return self.expression_type.to_sequence_type()


def analyze_expression(source: str,
                       parser: ta.XPathParserType | None = None,
                       context_item_type: str = 'item()',
                       variable_types: dict[str, str] | None = None
                       ) -> StaticAnalysisResult:
    """
    Performs a static type analysis of an XPath expression, reporting the
    provable type errors and the inferred sequence type of the expression.

    :param source: the XPath expression source string.
    :param parser: the parser instance that defines the static context, \
    an `XPath2Parser` instance for default.
    :param context_item_type: the sequence type of the context item, \
    'item()' for default.
    :param variable_types: an optional mapping of in-scope variable types \
    that extends the variable types of the parser's static context.
    :return: a `StaticAnalysisResult` instance. Expression errors, including \
    syntax errors and the type errors detected by the parser's static context \
    evaluation, are collected in the result instead of being raised.
    """
    if parser is None:
        from elementpath.xpath2 import XPath2Parser
        parser = XPath2Parser()

    try:
        root_token = parser.parse(source)
    except ElementPathError as err:
        return StaticAnalysisResult(StaticType.unknown(), [err])

    analyzer = ExpressionAnalyzer(parser, context_item_type, variable_types)
    expression_type = analyzer.analyze(root_token)
    return StaticAnalysisResult(expression_type, analyzer.errors)
