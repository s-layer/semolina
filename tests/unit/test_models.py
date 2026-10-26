"""
Tests for the SemanticView model base class.

A model is observed the way a user observes it: the fields it exposes, what ``metrics()`` and
``dimensions()`` return, the SQL its queries render (which is where the view name shows up),
its ``repr``, and the errors a bad definition raises, including how models inherit from one
another and from ``abstract=True`` bases. Query building itself is tested in
``test_query.py``.
"""

import pytest

from semolina import Dimension, Fact, Metric, SemanticView
from semolina.fields import Field


class TestModelDefinition:
    """A model names its view with the ``view=`` class keyword."""

    def test_the_view_parameter_names_the_view_queried(self):
        """``view="sales"`` is the relation every query of the model reads from."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        assert (
            Sales.query().metrics(Sales.revenue).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES"'
        )

    def test_model_definition_requires_view_parameter(self):
        """A model without ``view=`` is refused at class creation."""
        with pytest.raises(TypeError, match="must specify a view parameter"):

            class InvalidModel(SemanticView):  # pyright: ignore[reportUnusedClass]
                pass

    def test_model_definition_with_empty_view(self):
        """
        An empty view name is accepted today, and renders an empty identifier.

        Pinned as current behaviour, not endorsed: whether an empty view name should be
        refused at class creation is an open decision, and the day it is refused this test is
        the one to change.
        """

        class EmptyView(SemanticView, view=""):
            revenue = Metric()

        assert EmptyView.query().metrics(EmptyView.revenue).to_sql().endswith('\nFROM ""')


class TestFieldDeclaration:
    """Each field class declares a field of its own kind, named after its attribute."""

    @pytest.mark.parametrize("field_class", [Metric, Dimension, Fact])
    def test_a_declared_field_is_reachable_by_its_attribute_name(
        self, field_class: type[Field[object]]
    ):
        """The class attribute is the field, carries its kind, and knows its own name."""

        class Sales(SemanticView, view="sales"):
            thing = field_class()

        assert isinstance(Sales.thing, field_class)
        assert Sales.thing.name == "thing"


class TestModelFreezing:
    """A model class cannot be changed once it exists."""

    def test_cannot_add_fields_after_creation(self):
        """Adding a field to a finished model is refused."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        with pytest.raises(AttributeError, match="Cannot modify.*after class creation"):
            Sales.new_field = Metric()

    @pytest.mark.parametrize("attribute", ["_view_name", "_frozen"])
    def test_cannot_rewrite_the_models_own_bookkeeping(self, attribute: str):
        """
        Even the private attributes behind the view name and the freeze refuse assignment.

        Named by their private spelling because that is the only way a caller could reach
        them; the claim is that doing so fails loudly rather than silently re-pointing a model.
        """

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        with pytest.raises(AttributeError, match="Cannot modify.*after class creation"):
            setattr(Sales, attribute, "anything")


class TestMultipleModels:
    """Two models share nothing."""

    def test_multiple_models_have_separate_fields_and_views(self):
        """Each model reports its own fields and queries its own view."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        class Products(SemanticView, view="products"):
            price = Metric()
            category = Dimension()

        assert [m.name for m in Sales.metrics()] == ["revenue"]
        assert [m.name for m in Products.metrics()] == ["price"]
        assert [d.name for d in Products.dimensions()] == ["category"]
        assert Sales.dimensions() == []
        assert Sales.query().metrics(Sales.revenue).to_sql().endswith('\nFROM "SALES"')
        assert Products.query().metrics(Products.price).to_sql().endswith('\nFROM "PRODUCTS"')


class TestModelInheritance:
    """
    A model can extend another, or share fields through an ``abstract=True`` base.

    Every class queries its own view with its own fields: an inherited field is re-bound to
    the subclass, so ``Child.revenue`` reads from the child's view, not the parent's.
    """

    def test_a_subclass_inherits_fields_and_queries_its_own_view(self):
        """A concrete subclass has the parent's fields plus its own, and names its own view."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            country = Dimension()

        class SalesV2(Sales, view="sales_v2"):
            cost = Metric()

        assert [m.name for m in SalesV2.metrics()] == ["revenue", "cost"]
        assert [d.name for d in SalesV2.dimensions()] == ["country"]
        assert (
            (
                SalesV2.query().metrics(SalesV2.revenue, SalesV2.cost).dimensions(SalesV2.country)
            ).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue", AGG("COST") AS "cost", '
            '"COUNTRY" AS "country"\n'
            'FROM "SALES_V2"\n'
            "GROUP BY ALL"
        )

    def test_the_parent_is_unchanged_by_its_subclass(self):
        """Subclassing adds nothing to the parent and leaves its view alone."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        class SalesV2(Sales, view="sales_v2"):  # pyright: ignore[reportUnusedClass]
            cost = Metric()

        assert [m.name for m in Sales.metrics()] == ["revenue"]
        assert (
            Sales.query().metrics(Sales.revenue).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES"'
        )

    def test_an_inherited_field_belongs_to_the_subclass(self):
        """
        The parent's field object cannot be used in the subclass's query, and vice versa.

        Each class holds its own copy. If the subclass shared the parent's object, its query
        would read the parent's view.
        """

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        class SalesV2(Sales, view="sales_v2"):
            pass

        with pytest.raises(TypeError, match="different models"):
            SalesV2.query().metrics(Sales.revenue)
        with pytest.raises(TypeError, match="different models"):
            Sales.query().metrics(SalesV2.revenue)

    def test_a_subclass_field_overrides_the_parents(self):
        """Redeclaring a field in the subclass replaces the inherited one, in its place."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            cost = Metric()

        class SalesV2(Sales, view="sales_v2"):
            revenue = Metric(source="net_revenue")

        assert [m.name for m in SalesV2.metrics()] == ["revenue", "cost"]
        assert SalesV2.query().metrics(SalesV2.revenue).to_sql() == (
            'SELECT AGG("net_revenue") AS "revenue"\nFROM "SALES_V2"'
        )
        assert (
            Sales.query().metrics(Sales.revenue).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES"'
        )

    def test_the_nearest_override_wins_down_a_chain(self):
        """A grandchild inherits its parent's override, not the grandparent's original."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        class SalesV2(Sales, view="sales_v2"):
            revenue = Metric(source="net_revenue")

        class SalesV3(SalesV2, view="sales_v3"):
            pass

        assert SalesV3.query().metrics(SalesV3.revenue).to_sql() == (
            'SELECT AGG("net_revenue") AS "revenue"\nFROM "SALES_V3"'
        )

    def test_a_non_field_attribute_removes_an_inherited_field(self):
        """Shadowing an inherited field with something else drops it from the subclass."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            country = Dimension()

        class Global(Sales, view="global_sales"):
            country = None

        assert Global.dimensions() == []
        assert [m.name for m in Global.metrics()] == ["revenue"]

    def test_a_removed_field_stays_removed_down_a_chain(self):
        """
        A field a subclass removed does not come back on that subclass's own subclass.

        The removal leaves nothing in the middle class's fields, so a walk over each base's
        collected fields would find the grandparent's field again and restore it.
        """

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            country = Dimension()

        class Global(Sales, view="global_sales"):
            country = None

        class GlobalV2(Global, view="global_v2"):
            pass

        assert GlobalV2.dimensions() == []
        assert GlobalV2.country is None

    def test_a_plain_mixin_contributes_its_fields(self):
        """
        Fields on a mixin that is not a model are inherited like any other base's.

        The model's fields are whatever attribute lookup finds, so a mixin's field is re-bound
        to the model and queries the model's view.
        """

        class Money:
            revenue = Metric()

        class Sales(Money, SemanticView, view="sales"):
            pass

        assert [m.name for m in Sales.metrics()] == ["revenue"]
        assert (
            Sales.query().metrics(Sales.revenue).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES"'
        )

    def test_filtering_on_an_inherited_field_uses_the_subclass_view(self):
        """``.where()`` on an inherited dimension compiles against the subclass."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            country = Dimension()

        class SalesV2(Sales, view="sales_v2"):
            pass

        sql = SalesV2.query().metrics(SalesV2.revenue).where(SalesV2.country == "US").to_sql()
        assert (
            sql == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES_V2"\nWHERE "COUNTRY" = \'US\''
        )

    def test_a_subclass_is_frozen_too(self):
        """The subclass refuses new attributes after creation, as any model does."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()

        class SalesV2(Sales, view="sales_v2"):
            pass

        with pytest.raises(AttributeError, match="Cannot modify.*after class creation"):
            SalesV2.cost = Metric()


class TestAbstractModels:
    """An ``abstract=True`` base holds shared fields and names no view."""

    def test_models_share_fields_through_an_abstract_base(self):
        """Two views with the same shape declare it once."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()
            country = Dimension()

        class Sales(Commerce, view="sales"):
            pass

        class Returns(Commerce, view="returns"):
            refunds = Metric()

        assert (
            Sales.query().metrics(Sales.revenue).to_sql()
            == 'SELECT AGG("REVENUE") AS "revenue"\nFROM "SALES"'
        )
        assert Returns.query().metrics(Returns.revenue, Returns.refunds).to_sql() == (
            'SELECT AGG("REVENUE") AS "revenue", AGG("REFUNDS") AS "refunds"\nFROM "RETURNS"'
        )
        with pytest.raises(TypeError, match="different models"):
            Returns.query().metrics(Sales.revenue)

    def test_fields_combine_from_several_abstract_bases(self):
        """Abstract bases compose like mixins, in method resolution order."""

        class Money(SemanticView, abstract=True):
            revenue = Metric()

        class Geography(SemanticView, abstract=True):
            country = Dimension()

        class Sales(Money, Geography, view="sales"):
            pass

        assert [m.name for m in Sales.metrics()] == ["revenue"]
        assert [d.name for d in Sales.dimensions()] == ["country"]

    def test_a_model_built_on_an_abstract_base_executes(self, duckdb_pool: object):
        """A query of the concrete subclass runs against its view and returns its rows."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()
            country = Dimension()

        class Sales(Commerce, view="sales_view"):
            pass

        with Sales.query().metrics(Sales.revenue).dimensions(Sales.country).execute() as cursor:
            rows = cursor.fetchall_rows()

        assert {row.country: row.revenue for row in rows} == {"US": 1500, "CA": 2000}

    def test_an_abstract_model_cannot_be_queried(self):
        """It has no view, so asking it for a query is refused, naming the way out."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()

        with pytest.raises(TypeError, match="Commerce is abstract"):
            Commerce.query()

    def test_an_abstract_model_cannot_name_a_view(self):
        """``abstract=True`` with ``view=`` is a contradiction, refused at class creation."""
        with pytest.raises(TypeError, match="abstract.*cannot name a view"):

            class Commerce(SemanticView, abstract=True, view="sales"):  # pyright: ignore[reportUnusedClass]
                revenue = Metric()

    def test_a_concrete_subclass_of_an_abstract_base_still_needs_a_view(self):
        """Abstractness is not inherited: a subclass that names no view is refused."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()

        with pytest.raises(TypeError, match="must specify a view parameter"):

            class Sales(Commerce):  # pyright: ignore[reportUnusedClass]
                pass

    def test_an_abstract_model_lists_its_fields(self):
        """``metrics()`` and ``dimensions()`` work on an abstract base, for introspection."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()
            country = Dimension()

        assert [m.name for m in Commerce.metrics()] == ["revenue"]
        assert [d.name for d in Commerce.dimensions()] == ["country"]

    def test_an_abstract_models_repr_says_so(self):
        """The repr marks it abstract in place of a view."""

        class Commerce(SemanticView, abstract=True):
            revenue = Metric()

        assert repr(Commerce) == "<SemanticView 'Commerce' abstract metrics=['revenue']>"

    def test_the_base_class_cannot_be_queried(self):
        """``SemanticView`` itself is abstract in the same sense, and says so."""
        with pytest.raises(TypeError, match="SemanticView is abstract"):
            SemanticView.query()


class TestModelIntrospection:
    """``Model.metrics()`` and ``Model.dimensions()`` list the declared fields."""

    def test_metrics_returns_list_of_metrics(self):
        """``metrics()`` returns every Metric field and nothing else."""

        class Sales(SemanticView, view="sales"):
            revenue = Metric()
            cost = Metric()
            country = Dimension()

        metrics = Sales.metrics()
        assert all(isinstance(m, Metric) for m in metrics)
        assert [m.name for m in metrics] == ["revenue", "cost"]

    def test_metrics_returns_empty_list_if_no_metrics(self):
        """A model with no metrics returns an empty list."""

        class Locations(SemanticView, view="locations"):
            country = Dimension()
            region = Dimension()

        assert Locations.metrics() == []

    def test_dimensions_returns_dimensions_and_facts(self):
        """``dimensions()`` returns Dimension and Fact fields, not metrics."""

        class Orders(SemanticView, view="orders"):
            revenue = Metric()
            region = Dimension()
            date = Fact()

        dims = Orders.dimensions()
        assert all(isinstance(d, Dimension | Fact) for d in dims)
        assert [d.name for d in dims] == ["region", "date"]

    def test_dimensions_returns_empty_list_if_no_dimensions(self):
        """A model with no dimensions or facts returns an empty list."""

        class Metrics(SemanticView, view="metrics"):
            revenue = Metric()
            cost = Metric()

        assert Metrics.dimensions() == []


class TestReservedFieldNames:
    """A field may not shadow a model method."""

    @pytest.mark.parametrize("name", ["query", "metrics", "dimensions", "where", "execute"])
    def test_a_reserved_name_is_refused(self, name: str):
        """Declaring a field under a reserved name raises at class creation."""
        with pytest.raises(ValueError, match=f"'{name}' is reserved"):
            type("Invalid", (SemanticView,), {name: Metric()}, view="invalid")

    def test_error_message_lists_alternatives(self):
        """The error names both ways out: rename the attribute, or keep the column via source=."""
        with pytest.raises(ValueError) as exc_info:
            type("Invalid", (SemanticView,), {"query": Metric()}, view="invalid")

        error_msg = str(exc_info.value)
        assert "'query' is reserved" in error_msg
        assert "'query_field'" in error_msg
        assert "Metric(source='query')" in error_msg


class TestSemanticViewRepr:
    """A model's repr says which view it reads and which fields it declares."""

    def test_subclass_repr_shows_view_name(self) -> None:
        """The repr names the class and its view."""

        class Sales(SemanticView, view="sales_view"):
            revenue = Metric()
            country = Dimension()

        repr_str = repr(Sales)
        assert "SemanticView" in repr_str
        assert "'Sales'" in repr_str
        assert "sales_view" in repr_str

    def test_subclass_repr_shows_metrics(self) -> None:
        """The repr lists metric field names."""

        class Sales(SemanticView, view="sales_view"):
            revenue = Metric()
            cost = Metric()
            country = Dimension()

        repr_str = repr(Sales)
        assert "metrics=" in repr_str
        assert "'revenue'" in repr_str
        assert "'cost'" in repr_str

    def test_subclass_repr_shows_dimensions(self) -> None:
        """The repr lists dimension field names."""

        class Sales(SemanticView, view="sales_view"):
            revenue = Metric()
            country = Dimension()

        repr_str = repr(Sales)
        assert "dimensions=" in repr_str
        assert "'country'" in repr_str

    def test_subclass_repr_shows_facts(self) -> None:
        """The repr lists fact field names."""

        class Sales(SemanticView, view="sales_view"):
            unit_price = Fact()

        repr_str = repr(Sales)
        assert "facts=" in repr_str
        assert "'unit_price'" in repr_str

    def test_subclass_repr_omits_empty_categories(self) -> None:
        """Categories with no fields are left out."""

        class MetricsOnly(SemanticView, view="mo"):
            revenue = Metric()

        repr_str = repr(MetricsOnly)
        assert "dimensions=" not in repr_str
        assert "facts=" not in repr_str

    def test_base_class_repr_does_not_crash(self) -> None:
        """The base class, which has no view, still has a repr."""
        assert repr(SemanticView).startswith("<")


class TestModelDescription:
    """A model's ``description=`` class keyword, for its view's comment."""

    def test_the_description_is_kept(self) -> None:
        class Sales(SemanticView, view="sales", description="Sales by country"):
            revenue = Metric[float]()

        assert Sales._description == "Sales by country"

    def test_it_defaults_to_none(self) -> None:
        class Sales(SemanticView, view="sales"):
            revenue = Metric[float]()

        assert Sales._description is None

    def test_a_subclass_does_not_inherit_it(self) -> None:
        """The description describes one view; a subclass names another."""

        class Base(SemanticView, abstract=True, description="Base words"):
            revenue = Metric[float]()

        class Sales(Base, view="sales"):
            pass

        assert Sales._description is None
