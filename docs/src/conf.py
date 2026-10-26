"""Sphinx configuration for Semolina documentation."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Any

project = "Semolina"
copyright = "2026, Anentropic"
author = "Anentropic"

# Read from installed package metadata so pages can cite the version via ``|release|``
# instead of hardcoding one that goes stale at the next release.
try:
    release = _package_version("semolina")
except PackageNotFoundError:  # editable checkout without installed metadata
    release = "0.0.0+unknown"
version = release

extensions = [
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
    "sphinx_copybutton",
    "sphinx_design",
    "autoapi.extension",
]

exclude_patterns = ["_build"]

# Every cross-reference must resolve. This is what keeps hand-written pages honest: without
# it, a ref to a path that does not exist renders as unlinked plain text and the build stays
# green, which is how 131 dead refs to re-export paths (``semolina.SemanticView`` rather than
# ``semolina.models.SemanticView``) accumulated unnoticed.
nitpicky = True

# Targets that cannot resolve and are not defects. Everything here is either a third-party
# object with no published inventory, a typing artifact autoapi renders as a type, or a
# private name autoapi deliberately does not document. Anything NOT matching these is a real
# broken reference and fails the build.
# Sphinx matches these with ``re.fullmatch``, so each pattern must describe the whole target.
nitpick_ignore_regex = [
    # Third-party objects with no intersphinx inventory. pyarrow is mapped below instead;
    # adbc-drivers publishes an inventory but serves it 403 to non-browser clients.
    (
        "py:.*",
        r"(pandas|polars|typer|rich|pydantic|adbc_driver_manager|griffe|s_layer_semantic_formats)"
        r"\..*",
    ),
    # The stdlib inventory documents `ast.AST` and the node classes, but not the abstract
    # `ast.expr` base the codegen readers annotate their parameters with.
    ("py:class", r"ast\.expr"),
    # Typing artifacts and dunders that appear in signatures but are not documentable targets.
    ("py:.*", r"Ellipsis|T|_M|__del__"),
    # Private names: autoapi_options omits ``private-members``, so these are never emitted.
    ("py:.*", r"(\w+\.)*_\w+"),
    # Module-level type alias; autoapi emits it as py:data, so a py:class ref cannot match.
    ("py:class", r"WarehouseConfig"),
]

# Suppress warnings from autoapi-generated pages:
# - docutils: source docstrings use markdown-style backticks (legacy from mkdocstrings era)
# - autoapi.python_import_resolution: unresolvable type references in stubs
# - misc.highlighting_failure: Databricks $$ metric view syntax isn't standard SQL
suppress_warnings = [
    "docutils",
    "autoapi.python_import_resolution",
    "misc.highlighting_failure",
]

# -- Napoleon (Google-style docstrings)
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_use_rtype = False

# -- AutoAPI
autoapi_dirs = ["../../src/semolina"]
autoapi_type = "python"
autoapi_root = "reference/api"
autoapi_options = [
    "members",
    "show-inheritance",
    "show-module-summary",
]
autoapi_ignore = ["*conftest*", "*__main__*"]
autoapi_python_class_content = "class"
autoapi_member_order = "bysource"
autoapi_add_toctree_entry = False


def _hide_private_bases(
    _app: object, what: str, _name: str, obj: Any, _skip: bool, _options: object
) -> None:
    """
    Drop private base classes from a class's rendered "Bases:" line.

    ``show-inheritance`` would otherwise print ``Bases: semolina._cursor_base._CursorBase``
    above both cursors: a name from a module the reference does not document, which a
    reader can neither follow nor import. The base is how the cursors share code, not part
    of their API. Returning ``None`` leaves the skip decision to autoapi.
    """
    if what == "class":
        obj.bases = [
            base for base in obj.bases if not any(p.startswith("_") for p in base.split("."))
        ]


def setup(app: Any) -> None:
    """Register the autoapi hook above."""
    app.connect("autoapi-skip-member", _hide_private_bases)


# -- Intersphinx
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pyarrow": ("https://arrow.apache.org/docs", None),
}

# -- HTML output
html_theme = "shibuya"
html_title = "Semolina"
html_baseurl = "https://s-layer.github.io/semolina/"

html_static_path = ["_static"]
templates_path = ["_templates"]

html_css_files = [
    "css/s-layer.css",
]

html_copy_source = False
html_show_sourcelink = False

html_theme_options = {
    "accent_color": "orange",
    "github_url": "https://github.com/s-layer/semolina",
    "globaltoc_expand_depth": 1,
    "nav_links": [
        {"title": "Home", "url": "index"},
        {
            "title": "Tutorials",
            "url": "tutorials/index",
            "children": [
                {"title": "Installation", "url": "tutorials/installation"},
                {"title": "Your first query", "url": "tutorials/first-query"},
                {"title": "Shaping a report", "url": "tutorials/shaping-a-report"},
                {"title": "Serve a dashboard endpoint", "url": "tutorials/dashboard-api"},
                {
                    "title": "Test queries without a warehouse",
                    "url": "tutorials/testing-queries",
                },
                {
                    "title": "Generate models from your warehouse",
                    "url": "tutorials/warehouse-models",
                },
            ],
        },
        {
            "title": "How-To Guides",
            "url": "how-to/index",
            "children": [
                {
                    "title": "How to configure your warehouse backend",
                    "url": "how-to/backends",
                },
                {
                    "title": "How to manage engines and connection pools",
                    "url": "how-to/connection-pools",
                },
                {"title": "How to define models", "url": "how-to/models"},
                {"title": "How to build queries", "url": "how-to/queries"},
                {"title": "How to filter queries", "url": "how-to/filtering"},
                {
                    "title": "How to map results into Pydantic models with .into()",
                    "url": "how-to/typed-results",
                },
                {"title": "How to fetch results in bulk", "url": "how-to/streaming"},
                {"title": "How to run Semolina inside FastAPI", "url": "how-to/web-api"},
                {
                    "title": "How to generate Semolina model classes from warehouse views",
                    "url": "how-to/codegen",
                },
                {
                    "title": "How to generate a DTO class with semolina codegen-dto",
                    "url": "how-to/dto-codegen",
                },
                {
                    "title": "How to configure codegen credentials",
                    "url": "how-to/codegen-credentials",
                },
                {
                    "title": "How to test query code without a warehouse",
                    "url": "how-to/warehouse-testing",
                },
            ],
        },
        {
            "title": "Reference",
            "url": "reference/index",
            "children": [
                {"title": "Configuration file reference", "url": "reference/config"},
                {"title": "CLI reference", "url": "reference/cli"},
                {"title": "semolina", "url": "reference/api/semolina/index"},
            ],
        },
        {
            "title": "Explanation",
            "url": "explanation/index",
            "children": [
                {"title": "What is a semantic view?", "url": "explanation/semantic-views"},
                {
                    "title": "Warehouse types and what lands in your row",
                    "url": "explanation/type-fidelity",
                },
                {
                    "title": "Developing on DuckDB, deploying elsewhere",
                    "url": "explanation/duckdb-vs-warehouse",
                },
            ],
        },
        {"title": "Changelog", "url": "changelog/index"},
    ],
}
