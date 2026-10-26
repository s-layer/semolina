"""
Tests for :mod:`semolina.spec`: queries built from field names and plain data.

The central claim is that data names the same query the attribute API would build, so most
tests compare ``build_query(...)`` with the equivalent hand-written query (``Query`` and the
predicate nodes compare structurally). The schema tests then check that
:func:`query_spec_schema` and :func:`build_query` agree on what is valid.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from models import Sales

from semolina import Dimension, Fact, Metric, NullsOrdering, Row, SemanticView, SemolinaError
from semolina.filters import IsNull, Lookup, Predicate
from semolina.spec import (
    SemolinaSpecError,
    build_query,
    field_by_name,
    predicate_from_spec,
    query_spec_schema,
)


class Orders(SemanticView, view="orders"):
    """A model whose fields declare types, for the date conversion tests."""

    revenue = Metric[int]()
    order_count = Metric[int]()
    region = Dimension[str]()
    ordered_on = Dimension[datetime.date]()
    ordered_at = Dimension[datetime.datetime]()
    cutoff = Dimension[datetime.time]()
    unit_price = Fact[int]()
    untyped = Dimension()


def _spec_error(model: Any, spec: Any) -> SemolinaSpecError:
    """Build ``spec`` and return the SemolinaSpecError it raises."""
    with pytest.raises(SemolinaSpecError) as info:
        build_query(model, spec)
    return info.value


_NAMES = [*Orders._fields, "nope", "Revenue"]
_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-5, 5),
    st.sampled_from(["", "US", "2024-01-31", "2024-02-30", "17:45:00"]),
)
_JSON_VALUES = st.one_of(
    _JSON_SCALARS,
    st.lists(_JSON_SCALARS, max_size=3),
    st.dictionaries(st.sampled_from(["a", "field"]), st.integers(), max_size=1),
)


def _leaves() -> st.SearchStrategy[Any]:
    """Filter leaves over real and unknown fields, any op, any JSON value or none."""
    ops = st.sampled_from(["eq", "ne", "gt", "lte", "in", "between", "isnull", "like", "nope"])
    with_value = st.fixed_dictionaries(
        {"field": st.sampled_from(_NAMES), "op": ops, "value": _JSON_VALUES}
    )
    without_value = st.fixed_dictionaries({"field": st.sampled_from(_NAMES), "op": ops})
    return st.one_of(with_value, without_value, _JSON_SCALARS)


def _filters() -> st.SearchStrategy[Any]:
    """Filter trees up to a few levels deep, including empty and malformed composites."""
    return st.recursive(
        _leaves(),
        lambda children: st.one_of(
            st.fixed_dictionaries({"and": st.lists(children, max_size=3)}),
            st.fixed_dictionaries({"or": st.lists(children, max_size=3)}),
            st.fixed_dictionaries({"not": children}),
            st.fixed_dictionaries({"and": children}),
        ),
        max_leaves=6,
    )


_ORDER_ENTRIES = st.one_of(
    st.sampled_from(_NAMES),
    st.sampled_from(_NAMES).map(lambda n: f"-{n}"),
    st.fixed_dictionaries(
        {"field": st.sampled_from(_NAMES)},
        optional={
            "direction": st.sampled_from(["asc", "desc", "up"]),
            "nulls": st.sampled_from(["first", "last", "top"]),
        },
    ),
    st.integers(),
)


@st.composite
def _any_specs(draw: st.DrawFn) -> dict[str, Any]:
    """Query data with a random subset of keys, each drawn from valid and invalid values."""
    names = st.lists(st.sampled_from(_NAMES), max_size=3)
    candidates: dict[str, st.SearchStrategy[Any]] = {
        "metrics": st.one_of(names, st.just("revenue"), st.none()),
        "dimensions": names,
        "where": st.one_of(_filters(), st.lists(_filters(), max_size=2), st.none()),
        "order_by": st.lists(_ORDER_ENTRIES, max_size=3),
        "limit": st.one_of(st.integers(-1, 3), st.booleans(), st.just("5"), st.none()),
        "bogus": st.just(1),
    }
    keys = draw(st.lists(st.sampled_from(list(candidates)), unique=True, max_size=5))
    return {key: draw(candidates[key]) for key in keys}


_METRIC_NAMES = [n for n, f in Orders._fields.items() if isinstance(f, Metric)]
_DIMENSION_NAMES = [n for n, f in Orders._fields.items() if not isinstance(f, Metric)]
_CLEAN_SCALARS = st.one_of(
    st.booleans(), st.integers(-5, 5), st.sampled_from(["US", "2024-01-31", "17:45:00"])
)


def _clean_leaves() -> st.SearchStrategy[Any]:
    """Leaves whose value suits their op, on real fields."""
    field = st.sampled_from(list(Orders._fields))
    shapes: list[st.SearchStrategy[Any]] = [
        st.fixed_dictionaries(
            {
                "field": field,
                "op": st.sampled_from(["eq", "ne"]),
                "value": st.one_of(st.none(), _CLEAN_SCALARS),
            }
        ),
        st.fixed_dictionaries(
            {
                "field": field,
                "op": st.sampled_from(["gt", "gte", "lt", "lte"]),
                "value": _CLEAN_SCALARS,
            }
        ),
        st.fixed_dictionaries(
            {"field": field, "op": st.just("in"), "value": st.lists(_CLEAN_SCALARS, max_size=3)}
        ),
        st.fixed_dictionaries(
            {
                "field": field,
                "op": st.just("between"),
                "value": st.lists(_CLEAN_SCALARS, min_size=2, max_size=2),
            }
        ),
        st.fixed_dictionaries(
            {"field": field, "op": st.just("isnull")}, optional={"value": st.booleans()}
        ),
        st.fixed_dictionaries(
            {
                "field": field,
                "op": st.sampled_from(["like", "ilike", "startswith", "iexact"]),
                "value": st.text(max_size=3),
            }
        ),
    ]
    return st.one_of(shapes)


@st.composite
def _clean_specs(draw: st.DrawFn) -> dict[str, Any]:
    """Query data the schema should accept: real names in the right lists, well-formed filters."""
    spec: dict[str, Any] = {}
    metrics = draw(st.lists(st.sampled_from(_METRIC_NAMES), unique=True, max_size=2))
    dimensions = draw(st.lists(st.sampled_from(_DIMENSION_NAMES), unique=True, max_size=3))
    if not metrics and not dimensions:
        metrics = ["revenue"]
    if metrics:
        spec["metrics"] = metrics
    if dimensions:
        spec["dimensions"] = dimensions
    filters = st.recursive(
        _clean_leaves(),
        lambda children: st.one_of(
            st.fixed_dictionaries({"and": st.lists(children, min_size=1, max_size=3)}),
            st.fixed_dictionaries({"or": st.lists(children, min_size=1, max_size=3)}),
            st.fixed_dictionaries({"not": children}),
        ),
        max_leaves=5,
    )
    if draw(st.booleans()):
        spec["where"] = draw(st.one_of(filters, st.lists(filters, max_size=2)))
    if draw(st.booleans()):
        sortable = [*_METRIC_NAMES, *dimensions]
        spec["order_by"] = draw(
            st.lists(
                st.one_of(
                    st.sampled_from(sortable),
                    st.sampled_from(sortable).map(lambda n: f"-{n}"),
                    st.fixed_dictionaries(
                        {"field": st.sampled_from(sortable)},
                        optional={
                            "direction": st.sampled_from(["asc", "desc"]),
                            "nulls": st.sampled_from(["first", "last"]),
                        },
                    ),
                ),
                max_size=3,
            )
        )
    if draw(st.booleans()):
        spec["limit"] = draw(st.integers(1, 5))
    return spec


_specs = st.one_of(_clean_specs(), _any_specs())
"""Half well-formed query data, half anything, so both directions of agreement are tested."""


_ALL_OPS = [
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "between", "isnull", "like",
    "ilike", "startswith", "istartswith", "endswith", "iendswith", "iexact",
]  # fmt: skip
_MISSING = object()
_VALUE_CORPUS: list[Any] = [
    None, True, False, 0, 7, 1.5, "", "US", "%",
    [], [1], ["a"], [None], [1, 2], ["a", "b"], [1, None], [1, 2, 3], [[1]], [{"a": 1}],
    {}, {"a": 1},
]  # fmt: skip


def _builds(spec: dict[str, Any]) -> bool:
    """Whether ``build_query`` accepts ``spec`` for ``Orders``."""
    try:
        build_query(Orders, spec)
    except SemolinaSpecError:
        return False
    return True


class TestFieldByName:
    """``field_by_name`` returns the model's own descriptor."""

    @pytest.mark.parametrize("name", ["revenue", "country", "unit_price"])
    def test_returns_the_attribute(self, name: str):
        """The same object attribute access gives, for each kind of field."""
        assert field_by_name(Sales, name) is getattr(Sales, name)

    def test_a_subclass_gets_its_own_copy(self):
        """An inherited field is looked up on the subclass, where it is bound to it."""

        class Base(SemanticView, abstract=True):
            revenue = Metric()

        class Child(Base, view="child"):
            pass

        found = field_by_name(Child, "revenue")
        assert found is Child.revenue
        assert found.owner is Child

    def test_unknown_name_suggests_close_matches(self):
        """A typo names the nearest fields and lists the rest."""
        with pytest.raises(SemolinaSpecError, match=r"no field 'revnue'\. Did you mean 'revenue'"):
            field_by_name(Sales, "revnue")

    def test_unknown_name_lists_fields_when_nothing_is_close(self):
        """No suggestion, but the full list of names."""
        with pytest.raises(SemolinaSpecError) as info:
            field_by_name(Sales, "zzzz")
        assert "Did you mean" not in str(info.value)
        assert "'revenue', 'cost', 'country', 'region', 'unit_price'" in str(info.value)

    def test_source_name_is_not_a_field_name(self):
        """Lookup is by Python name, never by the warehouse column a field reads."""

        class Aliased(SemanticView, view="aliased"):
            revenue = Metric(source="REV_USD")

        with pytest.raises(SemolinaSpecError, match="no field 'REV_USD'"):
            field_by_name(Aliased, "REV_USD")

    def test_non_string_name(self):
        """A number from JSON is refused with its type."""
        with pytest.raises(SemolinaSpecError, match="must be a string, got int"):
            field_by_name(Sales, 3)  # pyright: ignore[reportArgumentType]

    def test_not_a_model(self):
        """A plain class is a programming error, not bad data."""
        with pytest.raises(TypeError, match="expected a SemanticView subclass"):
            field_by_name(dict, "x")  # pyright: ignore[reportArgumentType]

    def test_error_is_a_semolina_error_and_a_value_error(self):
        """One except clause catches it either way."""
        with pytest.raises(SemolinaError):
            field_by_name(Sales, "nope")
        with pytest.raises(ValueError):
            field_by_name(Sales, "nope")


class TestBuildQuery:
    """``build_query`` builds the query the attribute API would."""

    def test_full_spec_equals_attribute_query(self):
        """Every key at once."""
        built = build_query(
            Sales,
            {
                "metrics": ["revenue", "cost"],
                "dimensions": ["country", "unit_price"],
                "where": {"field": "region", "op": "eq", "value": "West"},
                "order_by": ["-revenue", "country"],
                "limit": 10,
            },
        )
        expected = (
            Sales.query()
            .metrics(Sales.revenue, Sales.cost)
            .dimensions(Sales.country, Sales.unit_price)
            .where(Sales.region == "West")
            .order_by(Sales.revenue.desc(), Sales.country)
            .limit(10)
        )
        assert built == expected
        assert built.to_sql() == expected.to_sql()

    def test_selection_order_is_kept(self):
        """Columns come back in the order the data names them."""
        built = build_query(Sales, {"metrics": ["cost", "revenue"]})
        assert built.selected_metrics == (Sales.cost, Sales.revenue)

    def test_using_names_the_engine(self):
        """``using=`` is passed through."""
        assert build_query(Sales, {"metrics": ["revenue"]}, using="wh").engine_name == "wh"

    def test_where_list_is_anded(self):
        """A list of filters must all hold."""
        built = build_query(
            Sales,
            {
                "metrics": ["revenue"],
                "where": [
                    {"field": "country", "op": "eq", "value": "US"},
                    {"field": "revenue", "op": "gt", "value": 100},
                ],
            },
        )
        assert built.filters == ((Sales.country == "US") & (Sales.revenue > 100))

    def test_where_null_means_no_filter(self):
        """An explicit ``null`` is the same as leaving the key out."""
        assert build_query(Sales, {"metrics": ["revenue"], "where": None}).filters is None

    def test_order_by_object_with_nulls(self):
        """The object form carries direction and NULL placement."""
        built = build_query(
            Sales,
            {
                "metrics": ["revenue"],
                "dimensions": ["country"],
                "order_by": [
                    {"field": "country", "nulls": "first"},
                    {"field": "revenue", "direction": "desc", "nulls": "last"},
                    {"field": "country", "direction": "asc"},
                ],
            },
        )
        assert built.ordering == (
            Sales.country.asc(NullsOrdering.FIRST),
            Sales.revenue.desc(NullsOrdering.LAST),
            Sales.country,
        )

    def test_order_by_unselected_metric_is_allowed(self):
        """A metric can sort without being selected, as in the attribute API."""
        built = build_query(
            Sales, {"metrics": ["revenue"], "dimensions": ["country"], "order_by": ["-cost"]}
        )
        assert built.ordering == (Sales.cost.desc(),)

    def test_abstract_model_cannot_be_queried(self):
        """The model's own refusal comes through."""

        class Base(SemanticView, abstract=True):
            revenue = Metric()

        with pytest.raises(TypeError, match="is abstract"):
            build_query(Base, {"metrics": ["revenue"]})

    def test_dimensions_only(self):
        """A dimension-only query, as the attribute API allows."""
        assert build_query(Sales, {"dimensions": ["country"]}) == Sales.query().dimensions(
            Sales.country
        )


class TestBuildQueryErrors:
    """Each mistake in the data raises with the path to it."""

    @pytest.mark.parametrize(
        ("spec", "path", "message"),
        [
            ({}, "", "selects at least one field"),
            ({"metrics": []}, "", "selects at least one field"),
            ({"metrics": ["revenue"], "limt": 3}, "", "unknown key 'limt'"),
            ({"metrics": "revenue"}, "metrics", "takes a list of field names"),
            ({"metrics": ["country"]}, "metrics[0]", "'country' is a dimension"),
            ({"dimensions": ["revenue"]}, "dimensions[0]", "'revenue' is a metric"),
            ({"metrics": ["revenue", "revenue"]}, "metrics[1]", "selected twice"),
            ({"metrics": ["revenue", "cots"]}, "metrics[1]", "Did you mean 'cost'"),
            ({"metrics": ["revenue"], "limit": 0}, "limit", "positive integer"),
            ({"metrics": ["revenue"], "limit": True}, "limit", "positive integer"),
            ({"metrics": ["revenue"], "limit": "5"}, "limit", "positive integer"),
            ({"metrics": ["revenue"], "order_by": "revenue"}, "order_by", "takes a list"),
            (
                {"metrics": ["revenue"], "order_by": ["country"]},
                "order_by[0]",
                "needs it in 'dimensions' too",
            ),
            (
                {"metrics": ["revenue"], "order_by": ["-unit_price"]},
                "order_by[0]",
                "ordering by fact 'unit_price'",
            ),
            ({"metrics": ["revenue"], "order_by": [3]}, "order_by[0]", "a field name or an"),
            (
                {"metrics": ["revenue"], "order_by": [{"field": "revenue", "dir": "desc"}]},
                "order_by[0]",
                "got 'dir'",
            ),
            ({"metrics": ["revenue"], "order_by": [{"direction": "desc"}]}, "order_by[0]", "needs"),
            (
                {"metrics": ["revenue"], "order_by": [{"field": "revenue", "direction": "down"}]},
                "order_by[0].direction",
                "'asc' or 'desc'",
            ),
            (
                {"metrics": ["revenue"], "order_by": [{"field": "revenue", "nulls": "top"}]},
                "order_by[0].nulls",
                "'first' or 'last'",
            ),
            ({"metrics": ["revenue"], "order_by": ["-nope"]}, "order_by[0]", "no field 'nope'"),
        ],
    )
    def test_error_path_and_message(self, spec: Any, path: str, message: str):
        """The error's ``path`` locates the mistake, and the message explains it."""
        error = _spec_error(Sales, spec)
        assert error.path == path
        assert message in str(error)
        if path:
            assert str(error).startswith(f"{path}: ")

    def test_spec_must_be_an_object(self):
        """A list where the query object should be."""
        error = _spec_error(Sales, ["revenue"])
        assert "a query is an object" in str(error)


class TestFilterLeaves:
    """Each ``op`` builds the predicate its field method builds."""

    @pytest.mark.parametrize(
        ("op", "value", "expected"),
        [
            ("eq", "US", Sales.country == "US"),
            ("eq", None, Sales.country == None),  # noqa: E711
            ("ne", "US", Sales.country != "US"),
            ("ne", None, Sales.country != None),  # noqa: E711
            ("gt", "M", Sales.country > "M"),
            ("gte", "M", Sales.country >= "M"),
            ("lt", "M", Sales.country < "M"),
            ("lte", "M", Sales.country <= "M"),
            ("in", ["US", "CA"], Sales.country.in_(["US", "CA"])),
            ("in", [], Sales.country.in_([])),
            ("between", ["A", "M"], Sales.country.between("A", "M")),
            ("isnull", True, Sales.country.isnull()),
            ("isnull", False, Sales.country.lookup(IsNull, False)),
            ("like", "U%", Sales.country.like("U%")),
            ("ilike", "u%", Sales.country.ilike("u%")),
            ("startswith", "U", Sales.country.startswith("U")),
            ("istartswith", "u", Sales.country.istartswith("u")),
            ("endswith", "S", Sales.country.endswith("S")),
            ("iendswith", "s", Sales.country.iendswith("s")),
            ("iexact", "us", Sales.country.iexact("us")),
        ],
    )
    def test_op_matches_field_method(self, op: str, value: Any, expected: Predicate):
        """Same node type, field and value."""
        built = predicate_from_spec(Sales, {"field": "country", "op": op, "value": value})
        assert built == expected
        assert type(built) is type(expected)

    def test_every_op_is_covered(self):
        """The module's operators are the ones these tests enumerate, no more and no fewer."""
        from semolina.spec import _OPS

        assert set(_OPS) == set(_ALL_OPS)

    def test_isnull_value_defaults_to_true(self):
        """``isnull`` without a value means IS NULL."""
        assert predicate_from_spec(Sales, {"field": "country", "op": "isnull"}) == (
            Sales.country.isnull()
        )

    def test_source_is_carried(self):
        """A ``source=`` field's predicate compiles against its source column."""

        class Aliased(SemanticView, view="aliased"):
            country = Dimension(source="COUNTRY_CODE")

        built = predicate_from_spec(Aliased, {"field": "country", "op": "eq", "value": "US"})
        assert built == (Aliased.country == "US")
        assert isinstance(built, Lookup)
        assert built.source == "COUNTRY_CODE"

    def test_metric_filter_compiles_to_having(self):
        """A metric leaf is a post-aggregation filter, as with the operator."""
        sql = build_query(
            Sales,
            {
                "metrics": ["revenue"],
                "dimensions": ["country"],
                "where": {"field": "revenue", "op": "gt", "value": 100},
            },
        ).to_sql()
        assert 'HAVING AGG("REVENUE") > 100' in sql

    @pytest.mark.parametrize(
        ("leaf", "path", "message"),
        [
            ({"op": "eq", "value": 1}, "where", "needs 'field'"),
            ({"field": "country", "value": 1}, "where", "needs 'op'"),
            ({"field": "country", "op": "eq"}, "where", "needs a 'value'"),
            ({"field": "country", "op": "equals", "value": 1}, "where.op", "unknown op"),
            ({"field": "country", "op": "eq", "value": 1, "x": 2}, "where", "got 'x'"),
            ({"field": "nope", "op": "eq", "value": 1}, "where.field", "no field 'nope'"),
            ({"field": "country", "op": "gt", "value": None}, "where.value", "null matches"),
            ({"field": "country", "op": "eq", "value": ["US"]}, "where.value", "single value"),
            ({"field": "country", "op": "eq", "value": {"a": 1}}, "where.value", "single"),
            ({"field": "country", "op": "in", "value": "US"}, "where.value", "takes a list"),
            ({"field": "country", "op": "in", "value": ["US", None]}, "where.value[1]", "null"),
            ({"field": "country", "op": "in", "value": [["US"]]}, "where.value[0]", "single"),
            ({"field": "country", "op": "between", "value": [1]}, "where.value", "[low, high]"),
            ({"field": "country", "op": "between", "value": [1, None]}, "where.value[1]", "null"),
            ({"field": "country", "op": "isnull", "value": "yes"}, "where.value", "true or"),
            ({"field": "country", "op": "like", "value": 5}, "where.value", "takes a string"),
            ("country = 'US'", "where", "a filter is an object"),
        ],
    )
    def test_leaf_errors(self, leaf: Any, path: str, message: str):
        """Each malformed leaf is located."""
        with pytest.raises(SemolinaSpecError) as info:
            predicate_from_spec(Sales, leaf)
        assert info.value.path == path
        assert message in str(info.value)

    def test_unknown_op_suggests_nearest(self):
        """``startwith`` is a near miss of ``startswith``."""
        with pytest.raises(SemolinaSpecError, match="Did you mean 'startswith'"):
            predicate_from_spec(Sales, {"field": "country", "op": "startwith", "value": "U"})


class TestFilterTrees:
    """``and``, ``or`` and ``not`` compose like ``&``, ``|`` and ``~``."""

    def test_nested_tree(self):
        """A tree three levels deep."""
        built = predicate_from_spec(
            Sales,
            {
                "or": [
                    {"field": "country", "op": "eq", "value": "US"},
                    {
                        "and": [
                            {"field": "region", "op": "eq", "value": "West"},
                            {"not": {"field": "country", "op": "in", "value": ["CA"]}},
                        ]
                    },
                ]
            },
        )
        assert built == (
            (Sales.country == "US") | ((Sales.region == "West") & ~Sales.country.in_(["CA"]))
        )

    def test_single_child_is_the_child(self):
        """An ``and`` of one filter is that filter."""
        leaf = {"field": "country", "op": "eq", "value": "US"}
        assert predicate_from_spec(Sales, {"and": [leaf]}) == (Sales.country == "US")
        assert predicate_from_spec(Sales, {"or": [leaf]}) == (Sales.country == "US")

    @pytest.mark.parametrize(
        ("spec", "path", "message"),
        [
            ({"and": []}, "where.and", "at least one filter"),
            ({"or": {"field": "country"}}, "where.or", "takes a list of filters"),
            ({"and": [{"field": "country"}]}, "where.and[0]", "needs 'op'"),
            (
                {"or": [{"field": "country", "op": "eq", "value": 1}, {"field": "x", "op": "eq"}]},
                "where.or[1].field",
                "no field 'x'",
            ),
            ({"not": {"field": "country", "op": "zz", "value": 1}}, "where.not.op", "unknown op"),
            ({"and": [], "or": []}, "where", "has no other keys"),
            ({"not": {}, "field": "country"}, "where", "has no other keys"),
        ],
    )
    def test_tree_errors(self, spec: Any, path: str, message: str):
        """A mistake deep in a tree is located."""
        with pytest.raises(SemolinaSpecError) as info:
            predicate_from_spec(Sales, spec)
        assert info.value.path == path
        assert message in str(info.value)


class TestDateValues:
    """An ISO string becomes the date or time type its field declares."""

    @pytest.mark.parametrize(
        ("field", "text", "expected"),
        [
            ("ordered_on", "2024-01-31", datetime.date(2024, 1, 31)),
            ("ordered_at", "2024-01-31T12:30:00", datetime.datetime(2024, 1, 31, 12, 30)),
            ("cutoff", "17:45:00", datetime.time(17, 45)),
        ],
    )
    def test_iso_string_is_converted(self, field: str, text: str, expected: object):
        """The predicate holds the typed value, so the dialect writes a typed literal."""
        built = predicate_from_spec(Orders, {"field": field, "op": "gte", "value": text})
        assert built == (getattr(Orders, field) >= expected)

    def test_in_and_between_values_are_converted(self):
        """Each item of a list is converted too."""
        between = predicate_from_spec(
            Orders, {"field": "ordered_on", "op": "between", "value": ["2024-01-01", "2024-01-31"]}
        )
        assert between == Orders.ordered_on.between(
            datetime.date(2024, 1, 1), datetime.date(2024, 1, 31)
        )
        within = predicate_from_spec(
            Orders, {"field": "ordered_on", "op": "in", "value": ["2024-01-01"]}
        )
        assert within == Orders.ordered_on.in_([datetime.date(2024, 1, 1)])

    def test_converted_value_renders_as_a_date_literal(self):
        """The difference reaches the SQL: a DATE literal rather than a string."""
        sql = build_query(
            Orders,
            {
                "metrics": ["revenue"],
                "where": {"field": "ordered_on", "op": "eq", "value": "2024-01-31"},
            },
        ).to_sql("snowflake")
        assert "DATE '2024-01-31'" in sql

    def test_malformed_date_is_refused_with_its_path(self):
        """Caught here rather than by the warehouse."""
        with pytest.raises(SemolinaSpecError) as info:
            predicate_from_spec(
                Orders,
                {"field": "ordered_on", "op": "between", "value": ["2024-01-01", "2024-02-30"]},
            )
        assert info.value.path == "where.value[1]"
        assert "'2024-02-30' is not an ISO date for 'ordered_on'" in str(info.value)

    @pytest.mark.parametrize("field", ["untyped", "region"])
    def test_other_fields_keep_the_string(self, field: str):
        """No declared type, or a non-date one, means no conversion."""
        built = predicate_from_spec(Orders, {"field": field, "op": "eq", "value": "2024-01-31"})
        assert isinstance(built, Lookup)
        assert built.value == "2024-01-31"

    def test_non_string_value_is_left_alone(self):
        """A date object from Python code passes through."""
        day = datetime.date(2024, 1, 31)
        built = predicate_from_spec(Orders, {"field": "ordered_on", "op": "eq", "value": day})
        assert isinstance(built, Lookup)
        assert built.value is day


class TestExecution:
    """A built query runs on DuckDB and returns what the data asks for."""

    def test_filter_order_limit_on_duckdb(self, duckdb_pool: Any):
        """Revenue by country in the West and East, largest first, top one."""
        query = build_query(
            Sales,
            {
                "metrics": ["revenue"],
                "dimensions": ["country"],
                "where": {"field": "region", "op": "in", "value": ["West", "East"]},
                "order_by": ["-revenue"],
                "limit": 1,
            },
        )
        with query.execute() as cursor:
            assert cursor.fetchall_rows() == [Row({"revenue": 2000, "country": "CA"})]

    def test_or_filter_on_duckdb(self, duckdb_pool: Any):
        """An ``or`` tree selects rows from both branches."""
        query = build_query(
            Sales,
            {
                "metrics": ["revenue"],
                "dimensions": ["region"],
                "where": {
                    "or": [
                        {"field": "country", "op": "eq", "value": "CA"},
                        {"field": "region", "op": "eq", "value": "East"},
                    ]
                },
                "order_by": ["region"],
            },
        )
        with query.execute() as cursor:
            assert cursor.fetchall_rows() == [
                Row({"revenue": 500, "region": "East"}),
                Row({"revenue": 2000, "region": "West"}),
            ]


class TestSchema:
    """``query_spec_schema`` describes exactly the data ``build_query`` accepts."""

    def test_schema_is_valid_draft_2020_12(self):
        """A JSON Schema validator accepts it, and it serialises to JSON."""
        schema = query_spec_schema(Orders)
        Draft202012Validator.check_schema(schema)
        assert json.loads(json.dumps(schema)) == schema

    def test_enums_list_the_models_own_names(self):
        """Metrics, dimensions (facts included) and filter fields, by role."""
        schema = query_spec_schema(Sales)
        props = schema["properties"]
        assert props["metrics"]["items"]["enum"] == ["revenue", "cost"]
        assert props["dimensions"]["items"]["enum"] == ["country", "region", "unit_price"]
        assert schema["$defs"]["order"]["properties"]["field"]["enum"] == [
            "revenue",
            "cost",
            "country",
            "region",
            "unit_price",
        ]

    def test_model_without_metrics_has_no_metrics_key(self):
        """An empty enum is not valid JSON Schema, so the key is left out."""

        class DimsOnly(SemanticView, view="dims_only"):
            country = Dimension()

        schema = query_spec_schema(DimsOnly)
        Draft202012Validator.check_schema(schema)
        assert "metrics" not in schema["properties"]
        validator = Draft202012Validator(schema)
        assert not validator.is_valid({"metrics": ["country"]})
        assert validator.is_valid({"dimensions": ["country"]})

    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"limit": 3},
            {"metrics": ["region"]},
            {"metrics": ["revenue"], "where": {"field": "region", "op": "gt", "value": None}},
            {"metrics": ["revenue"], "where": {"field": "region", "op": "between", "value": [1]}},
            {"metrics": ["revenue"], "where": {"and": []}},
            {"metrics": ["revenue"], "order_by": ["+revenue"]},
            {"metrics": ["revenue", "revenue"]},
        ],
    )
    def test_schema_and_builder_both_refuse(self, spec: dict[str, Any]):
        """Hand-picked invalid data, refused by both."""
        assert not Draft202012Validator(query_spec_schema(Orders)).is_valid(spec)
        _spec_error(Orders, spec)

    @settings(max_examples=500, deadline=None)
    @given(spec=_specs)
    def test_schema_and_builder_agree_on_whole_queries(self, spec: dict[str, Any]):
        """
        For generated query data, valid and invalid, the schema accepts it when it builds.

        Two refusals are allowed through: an ``order_by`` on an unselected dimension and a
        date string the field's type cannot parse, which its docstring names as what JSON
        Schema cannot say. The builder's message for each is checked, so that no other
        refusal hides behind them.
        """
        schema_ok = Draft202012Validator(query_spec_schema(Orders)).is_valid(spec)
        try:
            build_query(Orders, spec)
        except SemolinaSpecError as error:
            documented = "needs it in 'dimensions' too" in str(error) or "is not an ISO" in str(
                error
            )
            assert not schema_ok or documented, (spec, str(error))
        else:
            assert schema_ok, spec

    @pytest.mark.parametrize("op", [*_ALL_OPS, "nope"])
    def test_schema_and_builder_agree_on_every_leaf(self, op: str):
        """
        For every op and every value in a corpus of JSON shapes, the two agree on a leaf.

        Exhaustive rather than sampled, so a one-off gap (``between`` with one bound, say)
        cannot slip past a lucky draw. The field is ``untyped``, so no date conversion is
        involved.
        """
        validator = Draft202012Validator(query_spec_schema(Orders))
        for value in [*_VALUE_CORPUS, _MISSING]:
            leaf: dict[str, Any] = {"field": "untyped", "op": op}
            if value is not _MISSING:
                leaf["value"] = value
            spec = {"metrics": ["revenue"], "where": leaf}
            assert validator.is_valid(spec) == _builds(spec), leaf

    @pytest.mark.parametrize(
        ("key", "values"),
        [
            (
                "metrics",
                [None, [], ["revenue"], ["revenue", "revenue"], ["region"], ["nope"], "revenue", 1],
            ),
            ("dimensions", [None, [], ["region"], ["revenue"], ["region", "region"], [1], {}]),
            (
                "where",
                [
                    None,
                    [],
                    [{"field": "region", "op": "eq", "value": "x"}],
                    {"and": []},
                    {"or": [{"field": "region", "op": "isnull"}]},
                    {"not": {"field": "region", "op": "isnull"}},
                    {"not": []},
                    {"and": [], "or": []},
                    "region = 'x'",
                    [[]],
                ],
            ),
            (
                "order_by",
                [
                    None,
                    [],
                    ["revenue"],
                    ["-revenue"],
                    ["+revenue"],
                    ["region"],
                    [{"field": "revenue", "direction": "desc", "nulls": "last"}],
                    [{"field": "revenue", "direction": "down"}],
                    [{"field": "revenue", "extra": 1}],
                    [{"direction": "asc"}],
                    [1],
                    "revenue",
                ],
            ),
            ("limit", [None, 1, 5, 0, -1, True, False, 1.5, "5", [1]]),
            ("bogus", [None, 1]),
        ],
    )
    def test_schema_and_builder_agree_on_every_key(self, key: str, values: list[Any]):
        """
        Each top-level key, set to each value in its corpus beside an otherwise valid query.

        ``order_by: ["region"]`` (an unselected dimension) is the one documented gap and is
        left out of the corpus; it is covered by ``TestBuildQueryErrors``.
        """
        validator = Draft202012Validator(query_spec_schema(Orders))
        for value in values:
            spec: dict[str, Any] = {"metrics": ["order_count"], "dimensions": ["region"]}
            spec[key] = value
            assert validator.is_valid(spec) == _builds(spec), spec


def test_spec_error_is_exported_from_the_package_root():
    """``semolina.SemolinaSpecError`` is the class ``semolina.spec`` raises."""
    import semolina
    import semolina.spec

    assert "SemolinaSpecError" in semolina.__all__
    assert semolina.SemolinaSpecError is semolina.spec.SemolinaSpecError
