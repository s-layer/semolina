default:
    @just --list

# Set up the agent cli with GSD and skills based on agent name [claude|opencode]
setup-agent-cli agent="claude":
    @selected_agent="{{agent}}"; \
    selected_agent="${selected_agent#agent=}"; \
    case "${selected_agent}" in \
        claude) gsd_name="claude"; skills_name="claude-code" ;; \
        opencode) gsd_name="opencode"; skills_name="opencode" ;; \
        *) echo "Invalid agent '${selected_agent}'. Expected 'claude' or 'opencode'."; exit 1 ;; \
    esac; \
    npx get-shit-done-cc --"${gsd_name}" --local; \
    npx skills add abatilo/vimrc/plugins/abatilo-core/skills/diataxis-documentation -a "${skills_name}" -y; \
    npx skills add blader/humanizer -a "${skills_name}" -y

# Run all tests (unit + jaffle-shop mock) -- the same suite CI runs
#
# The sync is load-bearing. `uv sync --dev` alone installs none of the duckdb, snowflake,
# polars, pandas or arrowmodel extras, so dozens of test files quietly `importorskip` and
# this recipe reported green over a much smaller suite than CI ran. --extra all is what
# every CI test job syncs. CI adds `--cov`; coverage is not measured here (REL-05).
#
# `cd X && ...` rather than pushd/popd: just runs each line under `sh`, where pushd is not
# a builtin.
test:
    uv sync --locked --dev --extra all
    uv run pytest -n auto -m "not timing"
    uv run pytest -m timing
    cd semolina-jaffle-shop && PYTHONPATH=../tests uv run pytest -p ci_skips

# Regenerate the committed type-fidelity comparison artifact
type-fidelity:
    uv run python tests/type_fidelity_probe.py --write

# Build the docs site (strict mode)
docs-build:
    uv run sphinx-build -W docs/src docs/_build

# Serve the docs dev server (default port 8000)
docs-serve port="8000":
    uv run sphinx-autobuild docs/src docs/_build \
        --port {{port}} \
        --open-browser \
        --watch src/semolina \
        --re-ignore "reference/api"
