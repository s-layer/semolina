"""
Tests for field descriptors.

Tests cover:
- Field operator overloads returning Predicate nodes
- Named filter methods (between, in_, like, ilike, startswith, etc.)
- Escape hatch lookup() method for arbitrary Lookup subclasses
- Predicate composition via & (AND), | (OR), ~ (NOT)
- RuntimeError when field name is unset
- Field hashability preserved despite __eq__ override
"""

from collections.abc import Callable
from typing import Any

import pytest

from semolina import Dimension, Fact, Metric, SemanticView
from semolina.fields import Field, NullsOrdering, OrderTerm
from semolina.filters import (
    And,
    Between,
    EndsWith,
    Exact,
    Gt,
    Gte,
    IEndsWith,
    IExact,
    ILike,
    In,
    IsNull,
    IStartsWith,
    Like,
    Lt,
    Lte,
    Not,
    NotEqual,
    Or,
    StartsWith,
)


class TestFieldValidation:
    """Test field name validation."""

    def test_field_validation_valid_identifier(self):
        """Valid identifiers should be accepted."""

        class TestModel:
            pass

        field = Field()
        # Should not raise
        field.__set_name__(TestModel, "valid_name")
        assert field.name == "valid_name"
        assert field.owner == TestModel

    def test_field_validation_rejects_keywords(self):
        """Python keywords should be rejected."""

        class TestModel:
            pass

        field = Field()
        with pytest.raises(ValueError, match="Python keyword"):
            field.__set_name__(TestModel, "class")

        with pytest.raises(ValueError, match="Python keyword"):
            field.__set_name__(TestModel, "def")

        with pytest.raises(ValueError, match="Python keyword"):
            field.__set_name__(TestModel, "return")

    def test_field_validation_rejects_soft_keywords(self):
        """Python soft keywords should be rejected."""

        class TestModel:
            pass

        field = Field()
        with pytest.raises(ValueError, match="soft keyword"):
            field.__set_name__(TestModel, "match")

        with pytest.raises(ValueError, match="soft keyword"):
            field.__set_name__(TestModel, "case")

    def test_field_validation_rejects_reserved_names(self):
        """
        Reserved dict method names should be rejected.

        Reserved name validation lives in SemanticView.__init_subclass__
        (not Field.__set_name__) to avoid Python's RuntimeError wrapping.
        See TestReservedFieldNames in test_models.py for full coverage.
        """
        from semolina.models import SemanticView

        reserved_names = ["keys", "values", "items", "get", "pop", "update", "clear"]

        for name in reserved_names:
            with pytest.raises(ValueError, match="reserved"):
                type(name, (SemanticView,), {name: Field(), "__annotations__": {}}, view="test")

    def test_field_validation_rejects_invalid_identifiers(self):
        """Invalid Python identifiers should be rejected."""

        class TestModel:
            pass

        field = Field()
        with pytest.raises(ValueError, match="not a valid Python identifier"):
            field.__set_name__(TestModel, "123invalid")

        with pytest.raises(ValueError, match="not a valid Python identifier"):
            field.__set_name__(TestModel, "invalid-name")


class TestFieldDescriptorProtocol:
    """Test field descriptor protocol behavior."""

    def test_class_level_access_returns_field(self):
        """Class-level field access should return Field instance."""

        class TestModel:
            test_field = Field()

        field = TestModel.test_field
        assert isinstance(field, Field)
        assert field.name == "test_field"

    def test_instance_level_access_raises_error(self):
        """Instance-level field access should raise AttributeError."""

        class TestModel:
            test_field = Field()

        instance = TestModel()
        with pytest.raises(AttributeError, match="cannot be accessed from instances"):
            _ = instance.test_field

    def test_field_assignment_raises_error(self):
        """Field assignment should raise AttributeError."""

        class TestModel:
            test_field = Field()

        instance = TestModel()
        with pytest.raises(AttributeError, match="cannot be modified"):
            instance.test_field = "new_value"

    def test_field_deletion_raises_error(self):
        """Field deletion should raise AttributeError."""

        class TestModel:
            test_field = Field()

        instance = TestModel()
        with pytest.raises(AttributeError, match="cannot be deleted"):
            del instance.test_field


class TestFieldOrdering:
    """Test .asc() and .desc() methods on Field descriptors."""

    def test_desc_returns_order_term(self):
        """Sales.revenue.desc() should return OrderTerm with descending=True."""

        class TestModel:
            revenue = Metric()

        term = TestModel.revenue.desc()
        assert isinstance(term, OrderTerm)
        assert term.field is TestModel.revenue
        assert term.descending is True
        assert term.nulls == NullsOrdering.DEFAULT

    def test_asc_returns_order_term(self):
        """Sales.revenue.asc() should return OrderTerm with descending=False."""

        class TestModel:
            revenue = Metric()

        term = TestModel.revenue.asc()
        assert isinstance(term, OrderTerm)
        assert term.field is TestModel.revenue
        assert term.descending is False
        assert term.nulls == NullsOrdering.DEFAULT

    def test_desc_with_nulls_first(self):
        """Sales.revenue.desc(NullsOrdering.FIRST) should set nulls=FIRST."""

        class TestModel:
            revenue = Metric()

        term = TestModel.revenue.desc(NullsOrdering.FIRST)
        assert isinstance(term, OrderTerm)
        assert term.descending is True
        assert term.nulls == NullsOrdering.FIRST

    def test_asc_with_nulls_last(self):
        """Sales.revenue.asc(NullsOrdering.LAST) should set nulls=LAST."""

        class TestModel:
            revenue = Metric()

        term = TestModel.revenue.asc(NullsOrdering.LAST)
        assert isinstance(term, OrderTerm)
        assert term.descending is False
        assert term.nulls == NullsOrdering.LAST

    def test_desc_on_dimension(self):
        """Sales.country.desc() should work on Dimension fields."""

        class TestModel:
            country = Dimension()

        term = TestModel.country.desc()
        assert isinstance(term, OrderTerm)
        assert term.field is TestModel.country

    def test_desc_on_fact(self):
        """Sales.unit_price.desc() should work on Fact fields."""

        class TestModel:
            unit_price = Fact()

        term = TestModel.unit_price.desc()
        assert isinstance(term, OrderTerm)
        assert term.field is TestModel.unit_price

    def test_order_term_is_frozen(self):
        """OrderTerm attributes should not be modifiable after creation."""

        class TestModel:
            revenue = Metric()

        term = TestModel.revenue.desc()
        # Frozen dataclass should prevent modification
        with pytest.raises((AttributeError, TypeError)):
            term.descending = False  # type: ignore[misc]

    def test_order_term_repr(self):
        """repr(Sales.revenue.desc(NullsOrdering.FIRST)) should produce readable output."""

        class TestModel:
            revenue = Metric()

        term_desc_nulls_first = TestModel.revenue.desc(NullsOrdering.FIRST)
        repr_str = repr(term_desc_nulls_first)
        assert "OrderTerm" in repr_str
        assert "revenue" in repr_str
        assert "DESC" in repr_str
        assert "NULLS FIRST" in repr_str


class TestFieldOperators:
    """Test Field comparison operators returning Predicate nodes."""

    def test_eq_returns_exact(self):
        """Field == value should return Exact predicate."""

        class TestModel:
            country = Dimension()

        result = TestModel.country == "US"
        assert isinstance(result, Exact)
        assert result.field_name == "country"
        assert result.value == "US"

    def test_ne_returns_not_equal(self):
        """Field != value should return NotEqual(...)."""

        class TestModel:
            country = Dimension()

        result = TestModel.country != "US"
        assert isinstance(result, NotEqual)
        assert result.field_name == "country"
        assert result.value == "US"

    def test_lt_returns_lt(self):
        """Field < value should return Lt predicate."""

        class TestModel:
            revenue = Metric()

        result = TestModel.revenue < 1000
        assert isinstance(result, Lt)
        assert result.field_name == "revenue"
        assert result.value == 1000

    def test_le_returns_lte(self):
        """Field <= value should return Lte predicate."""

        class TestModel:
            revenue = Metric()

        result = TestModel.revenue <= 1000
        assert isinstance(result, Lte)
        assert result.field_name == "revenue"
        assert result.value == 1000

    def test_gt_returns_gt(self):
        """Field > value should return Gt predicate."""

        class TestModel:
            revenue = Metric()

        result = TestModel.revenue > 1000
        assert isinstance(result, Gt)
        assert result.field_name == "revenue"
        assert result.value == 1000

    def test_ge_returns_gte(self):
        """Field >= value should return Gte predicate."""

        class TestModel:
            revenue = Metric()

        result = TestModel.revenue >= 1000
        assert isinstance(result, Gte)
        assert result.field_name == "revenue"
        assert result.value == 1000

    def test_field_remains_hashable_despite_eq_override(self):
        """Field.__hash__ should be preserved despite __eq__ override."""

        class TestModel:
            country = Dimension()

        field = TestModel.country
        # Should be usable in sets and as dict keys
        field_set = {field}
        assert field in field_set

        field_dict = {field: "value"}
        assert field_dict[field] == "value"


class TestFieldNamedMethods:
    """Test named filter methods returning Predicate nodes."""

    def test_between_returns_between(self):
        """Field.between(lo, hi) should return Between with tuple value."""

        class TestModel:
            revenue = Metric()

        result = TestModel.revenue.between(100, 1000)
        assert isinstance(result, Between)
        assert result.field_name == "revenue"
        assert result.value == (100, 1000)

    def test_in_returns_in(self):
        """
        ``in_()`` returns an In predicate that keeps its own copy of the values.

        The caller's list is changed after the call. A predicate that held on to that list
        would change with it, and a query built earlier would quietly filter on the new
        values.
        """

        class TestModel:
            country = Dimension()

        values = ["US", "CA", "UK"]
        result = TestModel.country.in_(values)
        values.append("MX")

        assert isinstance(result, In)
        assert result.field_name == "country"
        assert result == TestModel.country.in_(["US", "CA", "UK"])

    @pytest.mark.parametrize("values", ["US", b"US"], ids=["str", "bytes"])
    def test_in_refuses_a_single_string(self, values: str | bytes):
        """
        A string is refused, not read as a collection of its characters.

        ``in_("US")`` would otherwise filter on ``IN ('U', 'S')`` and return the wrong rows
        without an error.
        """

        class TestModel:
            country = Dimension()

        with pytest.raises(TypeError, match=r"in_\(\) takes a collection of values"):
            TestModel.country.in_(values)

    def test_in_refuses_a_value_that_is_not_a_collection(self):
        """A single number is refused when the predicate is built, not when SQL is built."""

        class TestModel:
            revenue = Metric()

        with pytest.raises(TypeError, match=r"in_\(\) takes a collection of values"):
            TestModel.revenue.in_(5)  # pyright: ignore[reportArgumentType]

    def test_in_built_through_lookup_is_checked_the_same_way(self):
        """The ``lookup()`` escape hatch builds the same In, so it refuses a string too."""

        class TestModel:
            country = Dimension()

        with pytest.raises(TypeError, match=r"in_\(\) takes a collection of values"):
            TestModel.country.lookup(In, "US")

    def test_like_returns_like(self):
        """Field.like(pattern) should return Like predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.like("%Smith%")
        assert isinstance(result, Like)
        assert result.field_name == "name"
        assert result.value == "%Smith%"

    def test_ilike_returns_ilike(self):
        """Field.ilike(pattern) should return ILike predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.ilike("%smith%")
        assert isinstance(result, ILike)
        assert result.field_name == "name"
        assert result.value == "%smith%"

    def test_startswith_returns_startswith(self):
        """Field.startswith(prefix) should return StartsWith predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.startswith("A")
        assert isinstance(result, StartsWith)
        assert result.field_name == "name"
        assert result.value == "A"

    def test_istartswith_returns_istartswith(self):
        """Field.istartswith(prefix) should return IStartsWith predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.istartswith("a")
        assert isinstance(result, IStartsWith)
        assert result.field_name == "name"
        assert result.value == "a"

    def test_endswith_returns_endswith(self):
        """Field.endswith(suffix) should return EndsWith predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.endswith("son")
        assert isinstance(result, EndsWith)
        assert result.field_name == "name"
        assert result.value == "son"

    def test_iendswith_returns_iendswith(self):
        """Field.iendswith(suffix) should return IEndsWith predicate."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.iendswith("SON")
        assert isinstance(result, IEndsWith)
        assert result.field_name == "name"
        assert result.value == "SON"

    def test_iexact_returns_iexact(self):
        """Field.iexact(value) should return IExact predicate."""

        class TestModel:
            country = Dimension()

        result = TestModel.country.iexact("united states")
        assert isinstance(result, IExact)
        assert result.field_name == "country"
        assert result.value == "united states"

    def test_isnull_returns_isnull(self):
        """Field.isnull() should return IsNull with value=True."""

        class TestModel:
            country = Dimension()

        result = TestModel.country.isnull()
        assert isinstance(result, IsNull)
        assert result.field_name == "country"
        assert result.value is True

    def test_named_methods_on_metric(self):
        """Named methods should work on Metric fields."""

        class TestModel:
            revenue = Metric()

        assert isinstance(TestModel.revenue.between(0, 100), Between)
        assert isinstance(TestModel.revenue.in_([10, 20, 30]), In)
        assert isinstance(TestModel.revenue.isnull(), IsNull)

    def test_named_methods_on_fact(self):
        """Named methods should work on Fact fields."""

        class TestModel:
            price = Fact()

        assert isinstance(TestModel.price.between(0, 100), Between)
        assert isinstance(TestModel.price.in_([10, 20, 30]), In)
        assert isinstance(TestModel.price.isnull(), IsNull)


class TestFieldLookupEscapeHatch:
    """Test Field.lookup() escape hatch for arbitrary Lookup subclasses."""

    def test_lookup_returns_lookup_instance(self):
        """Field.lookup(cls, value) should return instance of given Lookup subclass."""

        class TestModel:
            country = Dimension()

        result = TestModel.country.lookup(Exact, "US")
        assert isinstance(result, Exact)
        assert result.field_name == "country"
        assert result.value == "US"

    def test_lookup_with_different_types(self):
        """lookup() should work with any Lookup subclass."""

        class TestModel:
            revenue = Metric()

        gt_result = TestModel.revenue.lookup(Gt, 1000)
        assert isinstance(gt_result, Gt)
        assert gt_result.field_name == "revenue"
        assert gt_result.value == 1000

        between_result = TestModel.revenue.lookup(Between, (10, 100))
        assert isinstance(between_result, Between)
        assert between_result.field_name == "revenue"
        assert between_result.value == (10, 100)

    def test_lookup_with_custom_subclass(self):
        """lookup() should work with user-defined Lookup subclasses."""
        from semolina.filters import Lookup

        class CustomLookup(Lookup[str]):
            """Custom lookup for testing."""

        class TestModel:
            name = Dimension()

        result = TestModel.name.lookup(CustomLookup, "test_value")
        assert isinstance(result, CustomLookup)
        assert isinstance(result, Lookup)
        assert result.field_name == "name"
        assert result.value == "test_value"


class TestFieldOperatorComposition:
    """Test Predicate composition via Field operators."""

    def test_and_composition(self):
        """(field == val) & (field > val) should return And(Exact, Gt)."""

        class TestModel:
            country = Dimension()
            revenue = Metric()

        result = (TestModel.country == "US") & (TestModel.revenue > 1000)
        assert isinstance(result, And)
        assert isinstance(result.left, Exact)
        assert isinstance(result.right, Gt)

    def test_or_composition(self):
        """(field == val) | (field == val) should return Or(Exact, Exact)."""

        class TestModel:
            country = Dimension()

        result = (TestModel.country == "US") | (TestModel.country == "CA")
        assert isinstance(result, Or)
        assert isinstance(result.left, Exact)
        assert isinstance(result.right, Exact)

    def test_not_composition(self):
        """~(field == val) should return Not(Exact(...))."""

        class TestModel:
            country = Dimension()

        result = ~(TestModel.country == "US")
        assert isinstance(result, Not)
        assert isinstance(result.inner, Exact)

    def test_complex_composition(self):
        """Complex nested composition should produce correct tree."""

        class TestModel:
            country = Dimension()
            revenue = Metric()

        result = ((TestModel.country == "US") | (TestModel.country == "CA")) & (
            TestModel.revenue > 1000
        )
        assert isinstance(result, And)
        assert isinstance(result.left, Or)
        assert isinstance(result.right, Gt)

    def test_named_method_composition(self):
        """Named methods should compose with operators."""

        class TestModel:
            country = Dimension()
            revenue = Metric()

        result = TestModel.country.in_(["US", "CA"]) & (TestModel.revenue > 1000)
        assert isinstance(result, And)
        assert isinstance(result.left, In)
        assert isinstance(result.right, Gt)


class TestFieldRuntimeError:
    """Every operator and filter method refuses a field that was never bound to a model."""

    @pytest.mark.parametrize(
        "use",
        [
            lambda f: f == "value",
            lambda f: f != "value",
            lambda f: f < 10,
            lambda f: f <= 10,
            lambda f: f > 10,
            lambda f: f >= 10,
            lambda f: f.between(1, 10),
            lambda f: f.in_([1, 2, 3]),
            lambda f: f.like("%test%"),
            lambda f: f.isnull(),
            lambda f: f.lookup(Exact, "value"),
        ],
        ids=["eq", "ne", "lt", "le", "gt", "ge", "between", "in_", "like", "isnull", "lookup"],
    )
    def test_raises_before_set_name(self, use: Callable[[Field[Any]], object]) -> None:
        """An unbound field has no name to filter on, and says so."""
        with pytest.raises(RuntimeError, match="before __set_name__"):
            use(Field())


class TestFieldRepr:
    """Test Field, Metric, Dimension, Fact repr."""

    def test_metric_repr(self) -> None:
        """Bound Metric should show Metric('field_name')."""

        class M(SemanticView, view="v"):
            revenue = Metric()

        assert repr(M.revenue) == "Metric('revenue')"

    def test_dimension_repr(self) -> None:
        """Bound Dimension should show Dimension('field_name')."""

        class M(SemanticView, view="v"):
            country = Dimension()

        assert repr(M.country) == "Dimension('country')"

    def test_fact_repr(self) -> None:
        """Bound Fact should show Fact('field_name')."""

        class M(SemanticView, view="v"):
            unit_price = Fact()

        assert repr(M.unit_price) == "Fact('unit_price')"

    def test_unbound_field_repr(self) -> None:
        """Unbound Field should show Field(unbound)."""
        f = Field()
        assert repr(f) == "Field(unbound)"

    def test_unbound_metric_repr(self) -> None:
        """Unbound Metric should show Metric(unbound)."""
        m = Metric()
        assert repr(m) == "Metric(unbound)"

    def test_bound_field_with_source_repr(self) -> None:
        """Bound Field with source= should show Field('name', source='SOURCE')."""

        class M(SemanticView, view="v"):
            order_id = Metric(source="ORDER_ID")

        assert repr(M.order_id) == "Metric('order_id', source='ORDER_ID')"

    def test_bound_field_no_source_repr(self) -> None:
        """Bound Field without source= should show Field('name') only."""

        class M(SemanticView, view="v"):
            revenue = Metric()

        assert repr(M.revenue) == "Metric('revenue')"


class TestFieldGeneric:
    """Test Generic[T] behavior on Field, Metric, Dimension, Fact."""

    def test_metric_subscript_in_class_definition(self) -> None:
        """Metric[int]() used as class descriptor should work and be usable."""

        class M(SemanticView, view="v"):
            revenue = Metric[int]()

        assert isinstance(M.revenue, Metric)
        assert isinstance(M.revenue, Field)
        assert M.revenue.name == "revenue"

    def test_subscript_class_level_access_returns_field(self) -> None:
        """Class-level access on subscript field returns the Field descriptor."""

        class M(SemanticView, view="v"):
            revenue = Metric[int]()

        # Must return the descriptor itself, not a T value
        field = M.revenue
        assert isinstance(field, Metric)


class TestFieldMetadata:
    """``label``, ``description``, ``synonyms`` and ``semantic_format`` on a field."""

    CURRENCY: dict[str, Any] = {"value": {"kind": "currency", "currency": "USD"}}

    def test_defaults_are_empty(self) -> None:
        field = Metric[float]()
        assert field.label is None
        assert field.description is None
        assert field.synonyms == ()
        assert field.semantic_format is None

    def test_arguments_are_kept(self) -> None:
        field = Dimension[str](
            "COUNTRY", label="Country", description="Ship-to country", synonyms=["nation"]
        )
        assert field.source == "COUNTRY"
        assert field.label == "Country"
        assert field.description == "Ship-to country"
        assert field.synonyms == ("nation",)

    def test_a_bare_string_is_not_taken_as_synonyms(self) -> None:
        """``synonyms="nation"`` would otherwise read as six one-letter synonyms."""
        with pytest.raises(TypeError, match="synonyms"):
            Dimension[str](synonyms="nation")

    def test_a_format_mapping_is_validated_into_a_block(self) -> None:
        from s_layer_semantic_formats import SemanticFormat, validate_block

        field = Metric[float](semantic_format=self.CURRENCY)

        assert isinstance(field.semantic_format, SemanticFormat)
        assert field.semantic_format == validate_block(self.CURRENCY, "metric")

    def test_a_block_is_accepted_as_is(self) -> None:
        from s_layer_semantic_formats import validate_block

        block = validate_block(self.CURRENCY, "fact")
        assert Fact[float](semantic_format=block).semantic_format == block

    def test_a_kind_for_another_member_type_is_rejected(self) -> None:
        from s_layer_semantic_formats import SLayerValidationError

        with pytest.raises(SLayerValidationError, match="not a dimension"):
            Dimension[str](semantic_format=self.CURRENCY)

    def test_an_invalid_block_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="value"):
            Metric[float](semantic_format={"kind": "currency"})

    @pytest.mark.parametrize("key", ["label", "synonyms"])
    def test_a_block_carrying_a_field_argument_is_rejected(self, key: str) -> None:
        """One place for each: the field's own argument, not a copy inside the block."""
        carried = {**self.CURRENCY, key: "Revenue" if key == "label" else ["sales"]}
        with pytest.raises(ValueError, match=f"pass {key}="):
            Metric[float](semantic_format=carried)
