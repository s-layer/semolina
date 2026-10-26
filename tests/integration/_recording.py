"""
Record-mode helpers for the integration fixtures, importable by tests.

Kept out of ``conftest.py`` so a test can import them by a name no other module shares
(``tests/conftest.py`` would shadow a bare ``import conftest``). Not collected: the
name does not match ``test_*``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adbc_poolhouse import SnowflakeConfig


TAGGED_REVENUE_SPEC = '{"value":{"currency":"USD"}}'
"""The ``S_LAYER_SPEC`` tag on ``tagged_view``'s revenue, on both warehouses."""


def snowflake_query_config(
    base_config: SnowflakeConfig,
    *,
    schema_name: str,
    session_database: str | None,
) -> SnowflakeConfig:
    """
    Return the config the recording query pool connects with.

    The Snowflake ADBC driver ignores ``schema`` unless ``database`` is also set, and leaves
    the session in the user's default namespace. So the pool is given the database the
    native session resolved, which is where the temp schema was created, whether or not
    ``[connections.snowflake]`` names one.

    Args:
        base_config: The ``[connections.snowflake]`` config.
        schema_name: The temp schema the fixture created.
        session_database: ``CURRENT_DATABASE()`` of the native session that created it.

    Returns:
        ``base_config`` pointed at ``session_database.schema_name``.

    Raises:
        ValueError: If the session had no current database, so there is none to pin.
    """
    if session_database is None:
        msg = (
            "The native Snowflake session has no current database, so the query pool "
            "would ignore the temp schema. Set `database` in [connections.snowflake] "
            "or give your Snowflake user a default namespace."
        )
        raise ValueError(msg)
    return base_config.model_copy(update={"database": session_database, "schema_": schema_name})
