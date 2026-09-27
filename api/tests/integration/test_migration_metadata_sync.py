"""Alembic head and ``migrations/model_registry`` must describe the same schema.

``migrations/env.py`` imports ``migrations/model_registry``, which imports each
adapters model module so its tables register on the shared ``Base.metadata`` that
Alembic autogenerate diffs against. If a module is forgotten (as ``backup_models``
was), the table exists in the database after ``upgrade head`` but is absent from
the registry, so autogenerate would emit a spurious drop.

The registry tables must be read in a *subprocess* that imports ONLY
``model_registry`` -- not the app, not this test package's conftest. ``Base.metadata``
is a process-global: many test modules import the app (which imports every model
module), so in this process it is fully populated regardless of what ``env.py``
registers. Asserting against it in-process would pass vacuously even if the
``env.py`` fix were reverted. The subprocess imports the registration path in
isolation, so the comparison reflects what migrations actually pull in.

The parity test upgrades the real migration chain in ``public``. It then creates
the ORM metadata in a separate schema in that same PostgreSQL database and
reflects both schemas. The reference schema lets PostgreSQL normalize types,
defaults, CHECK expressions, and index predicates on both sides; the schema under
test still comes exclusively from ``alembic upgrade head``, never from
``metadata.create_all``. The reflected contracts compare columns, primary keys,
foreign keys, unique/check constraints, and indexes with property-level
diagnostics. The same round trip then downgrades to base and proves that only
Alembic's bookkeeping table remains.

Intentional differences are exact, reasoned allow-list entries. Alembic's own
bookkeeping table is the only one: no table family, constraint class, or column
property is broadly excluded.

Database-dependent tests run only when ``MCD_TEST_DATABASE_URL`` is set (the CI
Postgres service); skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from sqlalchemy import Connection, Integer, MetaData, String, create_engine, inspect
from sqlalchemy.engine.interfaces import (
    ReflectedCheckConstraint,
    ReflectedColumn,
    ReflectedConstraint,
    ReflectedForeignKeyConstraint,
    ReflectedIndex,
    ReflectedPrimaryKeyConstraint,
    ReflectedUniqueConstraint,
)
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

_needs_db = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

# Directory holding ``model_registry`` (alongside ``env.py``), added to the
# subprocess's path so it can be imported standalone -- exactly as ``env.py``
# imports it (``prepend_sys_path = src:migrations`` in alembic.ini).
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# ``Base.metadata`` is materialized here so PostgreSQL canonicalizes both sides
# of the comparison. The schema is dropped in ``finally`` and each pytest session
# owns a separate scratch database (tests/integration/README.md).
_METADATA_SCHEMA = "orm_metadata_parity"

_ABSENT = "<absent>"


@dataclass(frozen=True)
class _SchemaDifference:
    property: str
    metadata: str
    migrated: str


@dataclass(frozen=True)
class _SchemaContract:
    tables: Mapping[str, Mapping[str, object]]


# Exact differences only. Keeping the observed values in the key makes an entry
# stale (and therefore failing) if the backend behavior changes underneath it.
_INTENTIONAL_DIFFERENCES: Mapping[_SchemaDifference, str] = {
    _SchemaDifference(
        property="table.alembic_version",
        metadata=_ABSENT,
        migrated="'present'",
    ): (
        "Alembic creates its version bookkeeping table outside ORM metadata; "
        "application code never maps it."
    ),
}

# Snippet run in a fresh interpreter: import ONLY the registration path, then
# print the tables it registered. No app import, no test conftest.
_REGISTRY_SNIPPET = (
    "import sys; "
    f"sys.path.insert(0, {str(_MIGRATIONS_DIR)!r}); "
    "from model_registry import target_metadata; "
    "print('\\n'.join(sorted(target_metadata.tables)))"
)


def _registry_tables() -> set[str]:
    result = subprocess.run(
        [sys.executable, "-c", _REGISTRY_SNIPPET],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def _target_metadata() -> MetaData:
    sys.path.insert(0, str(_MIGRATIONS_DIR))
    from model_registry import target_metadata

    return target_metadata


def _required_name(
    reflected: ReflectedConstraint | ReflectedIndex,
    *,
    table: str,
    kind: str,
) -> str:
    name = reflected["name"]
    assert isinstance(name, str), f"{table} has an unnamed {kind}"
    return name


def _as_optional_string(value: object, *, property_name: str) -> str | None:
    assert value is None or isinstance(value, str), (
        f"{property_name} reflected as unexpected value {value!r}"
    )
    return value


def _normalize_sql(
    value: str | None,
    *,
    local_schema: str | None,
    normalize_local_sequence: bool = False,
) -> str | None:
    """Normalize only the test schema on a reflected serial-sequence default.

    PostgreSQL already canonicalizes reflected SQL. General whitespace or
    string-literal rewriting would erase semantic differences, so every other
    expression is returned byte-for-byte.
    """

    if value is None or local_schema is None or not normalize_local_sequence:
        return value
    prefix = f"nextval('{local_schema}."
    suffix = "'::regclass)"
    if not value.startswith(prefix) or not value.endswith(suffix):
        return value
    sequence_name = value[len(prefix) : -len(suffix)]
    if not sequence_name or "'" in sequence_name:
        return value
    return f"nextval('{sequence_name}'::regclass)"


def _normalize_keyword(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


def _normalize_type(sync_conn: Connection, reflected_type: object) -> str:
    compile_type = getattr(reflected_type, "compile", None)
    assert callable(compile_type), f"uncompilable reflected type {reflected_type!r}"
    return " ".join(str(compile_type(dialect=sync_conn.dialect)).split())


def _record_foreign_key(
    properties: dict[str, object],
    foreign_key: ReflectedForeignKeyConstraint,
    *,
    table: str,
    local_schema: str | None,
) -> None:
    name = _required_name(foreign_key, table=table, kind="foreign key")
    prefix = f"foreign_key.{name}"
    referred_schema = foreign_key["referred_schema"]
    if referred_schema == local_schema:
        referred_schema = None
    properties[f"{prefix}.columns"] = tuple(foreign_key["constrained_columns"])
    properties[f"{prefix}.referred_schema"] = referred_schema
    properties[f"{prefix}.referred_table"] = foreign_key["referred_table"]
    properties[f"{prefix}.referred_columns"] = tuple(foreign_key["referred_columns"])
    options = foreign_key.get("options", {})
    for option in ("ondelete", "onupdate", "deferrable", "initially", "match"):
        properties[f"{prefix}.{option}"] = _normalize_keyword(options.get(option))


def _record_index(
    properties: dict[str, object],
    index: ReflectedIndex,
    *,
    table: str,
    local_schema: str | None,
) -> None:
    name = _required_name(index, table=table, kind="index")
    prefix = f"index.{name}"
    expressions = tuple(
        _normalize_sql(expression, local_schema=local_schema)
        for expression in index.get("expressions", [])
    )
    dialect_options = index.get("dialect_options", {})
    predicate = _as_optional_string(
        dialect_options.get("postgresql_where"),
        property_name=f"{table}.{name}.postgresql_where",
    )
    properties[f"{prefix}.unique"] = index["unique"]
    properties[f"{prefix}.columns"] = tuple(index["column_names"])
    properties[f"{prefix}.expressions"] = expressions
    properties[f"{prefix}.predicate"] = _normalize_sql(
        predicate, local_schema=local_schema
    )


def _table_contract(
    sync_conn: Connection,
    inspector: Inspector,
    table: str,
    *,
    schema: str | None,
) -> Mapping[str, object]:
    properties: dict[str, object] = {}

    for column in inspector.get_columns(table, schema=schema):
        prefix = f"column.{column['name']}"
        properties[f"{prefix}.type"] = _normalize_type(sync_conn, column["type"])
        properties[f"{prefix}.nullable"] = column["nullable"]
        properties[f"{prefix}.server_default"] = _normalize_sql(
            column["default"],
            local_schema=schema,
            normalize_local_sequence=True,
        )

    primary_key = inspector.get_pk_constraint(table, schema=schema)
    if primary_key["constrained_columns"]:
        name = _required_name(primary_key, table=table, kind="primary key")
        properties[f"primary_key.{name}.columns"] = tuple(
            primary_key["constrained_columns"]
        )

    for foreign_key in inspector.get_foreign_keys(table, schema=schema):
        _record_foreign_key(
            properties,
            foreign_key,
            table=table,
            local_schema=schema,
        )

    for unique_constraint in inspector.get_unique_constraints(table, schema=schema):
        name = _required_name(unique_constraint, table=table, kind="unique constraint")
        prefix = f"unique_constraint.{name}"
        properties[f"{prefix}.columns"] = tuple(unique_constraint["column_names"])

    for check_constraint in inspector.get_check_constraints(table, schema=schema):
        name = _required_name(check_constraint, table=table, kind="check constraint")
        properties[f"check_constraint.{name}.expression"] = _normalize_sql(
            check_constraint["sqltext"], local_schema=schema
        )

    for index in inspector.get_indexes(table, schema=schema):
        _record_index(
            properties,
            index,
            table=table,
            local_schema=schema,
        )

    return properties


def _schema_contract(sync_conn: Connection, *, schema: str | None) -> _SchemaContract:
    inspector = inspect(sync_conn)
    tables = {
        table: _table_contract(
            sync_conn,
            inspector,
            table,
            schema=schema,
        )
        for table in inspector.get_table_names(schema=schema)
    }
    return _SchemaContract(tables=tables)


def _create_metadata_schema(sync_conn: Connection) -> None:
    translated = sync_conn.execution_options(
        schema_translate_map={None: _METADATA_SCHEMA}
    )
    _target_metadata().create_all(translated, checkfirst=False)


def _reflect_metadata_schema(sync_conn: Connection) -> _SchemaContract:
    return _schema_contract(sync_conn, schema=_METADATA_SCHEMA)


def _reflect_migrated_schema(sync_conn: Connection) -> _SchemaContract:
    return _schema_contract(sync_conn, schema=None)


def _schema_differences(
    metadata: Mapping[str, object],
    migrated: Mapping[str, object],
    *,
    prefix: str = "",
) -> tuple[_SchemaDifference, ...]:
    differences: list[_SchemaDifference] = []
    for property_name in sorted(metadata.keys() | migrated.keys()):
        metadata_value = (
            repr(metadata[property_name]) if property_name in metadata else _ABSENT
        )
        migrated_value = (
            repr(migrated[property_name]) if property_name in migrated else _ABSENT
        )
        if metadata_value != migrated_value:
            differences.append(
                _SchemaDifference(
                    property=f"{prefix}{property_name}",
                    metadata=metadata_value,
                    migrated=migrated_value,
                )
            )
    return tuple(differences)


def _contract_differences(
    metadata: _SchemaContract, migrated: _SchemaContract
) -> tuple[_SchemaDifference, ...]:
    metadata_tables = {f"table.{table}": "present" for table in metadata.tables}
    migrated_tables = {f"table.{table}": "present" for table in migrated.tables}
    differences = list(_schema_differences(metadata_tables, migrated_tables))

    for table in sorted(metadata.tables.keys() & migrated.tables.keys()):
        differences.extend(
            _schema_differences(
                metadata.tables[table],
                migrated.tables[table],
                prefix=f"table.{table}.",
            )
        )
    return tuple(differences)


def _format_schema_differences(
    differences: Sequence[_SchemaDifference],
) -> str:
    return "\n".join(
        f"- {difference.property}: ORM metadata={difference.metadata}; "
        f"migrated database={difference.migrated}"
        for difference in differences
    )


@_needs_db
async def test_migrated_schema_matches_orm_metadata() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                DropSchema(_METADATA_SCHEMA, cascade=True, if_exists=True)
            )
            await conn.execute(CreateSchema(_METADATA_SCHEMA))
            await conn.run_sync(_create_metadata_schema)
        async with engine.connect() as conn:
            metadata_contract = await conn.run_sync(_reflect_metadata_schema)
            migrated_contract = await conn.run_sync(_reflect_migrated_schema)
    finally:
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    DropSchema(_METADATA_SCHEMA, cascade=True, if_exists=True)
                )
        finally:
            await engine.dispose()
            await downgrade_base(_DB_URL)

    base_engine = create_async_engine(_DB_URL)
    try:
        async with base_engine.connect() as conn:
            base_contract = await conn.run_sync(_reflect_migrated_schema)
    finally:
        await base_engine.dispose()

    assert set(base_contract.tables) == {"alembic_version"}, (
        "downgrade base must leave only Alembic bookkeeping; found "
        f"{set(base_contract.tables)}"
    )

    registered_tables = _registry_tables()
    reflected_metadata_tables = set(metadata_contract.tables)
    assert registered_tables == reflected_metadata_tables, (
        "migrations/model_registry differs when imported in isolation; "
        f"missing from isolated registry: "
        f"{reflected_metadata_tables - registered_tables}; extra in isolated "
        f"registry: {registered_tables - reflected_metadata_tables}"
    )

    differences = _contract_differences(metadata_contract, migrated_contract)
    observed = set(differences)
    allowed = set(_INTENTIONAL_DIFFERENCES)
    unexpected = tuple(sorted(observed - allowed, key=lambda item: item.property))
    stale_allowlist = tuple(sorted(allowed - observed, key=lambda item: item.property))

    assert not unexpected, (
        "migrated PostgreSQL schema diverges from ORM metadata:\n"
        f"{_format_schema_differences(unexpected)}"
    )
    assert not stale_allowlist, (
        "schema parity allow-list entries are stale and must be removed:\n"
        + "\n".join(
            f"- {difference.property}: {_INTENTIONAL_DIFFERENCES[difference]}"
            for difference in stale_allowlist
        )
    )


def test_sql_normalization_preserves_literal_whitespace() -> None:
    expression = "value = 'meaningful  repeated  spaces'"

    assert (
        _normalize_sql(
            expression,
            local_schema=_METADATA_SCHEMA,
            normalize_local_sequence=True,
        )
        == expression
    )


def test_sql_normalization_preserves_unrelated_schema_text() -> None:
    expression = f"value = '{_METADATA_SCHEMA}.not_a_sequence'"

    assert (
        _normalize_sql(
            expression,
            local_schema=_METADATA_SCHEMA,
            normalize_local_sequence=True,
        )
        == expression
    )


def test_sql_normalization_removes_reference_schema_from_sequence_default() -> None:
    expression = f"nextval('{_METADATA_SCHEMA}.login_attempt_id_seq'::regclass)"

    assert (
        _normalize_sql(
            expression,
            local_schema=_METADATA_SCHEMA,
            normalize_local_sequence=True,
        )
        == "nextval('login_attempt_id_seq'::regclass)"
    )


_COMPARED_PROPERTIES = (
    "column.id.type",
    "column.id.nullable",
    "column.id.server_default",
    "primary_key.pk_example.columns",
    "foreign_key.fk_example_parent.columns",
    "foreign_key.fk_example_parent.referred_schema",
    "foreign_key.fk_example_parent.referred_table",
    "foreign_key.fk_example_parent.referred_columns",
    "foreign_key.fk_example_parent.ondelete",
    "foreign_key.fk_example_parent.onupdate",
    "foreign_key.fk_example_parent.deferrable",
    "foreign_key.fk_example_parent.initially",
    "foreign_key.fk_example_parent.match",
    "unique_constraint.uq_example_name.columns",
    "check_constraint.ck_example_state.expression",
    "index.ix_example_name.unique",
    "index.ix_example_name.columns",
    "index.ix_example_name.expressions",
    "index.ix_example_name.predicate",
)


def _reflected_table_contract(*, mutation: str | None = None) -> Mapping[str, object]:
    column: ReflectedColumn = {
        "name": "id",
        "type": String(),
        "nullable": False,
        "default": "'expected'::character varying",
    }
    primary_key: ReflectedPrimaryKeyConstraint = {
        "name": "pk_example",
        "constrained_columns": ["id"],
    }
    foreign_key: ReflectedForeignKeyConstraint = {
        "name": "fk_example_parent",
        "constrained_columns": ["parent_id"],
        "referred_schema": None,
        "referred_table": "parent",
        "referred_columns": ["id"],
        "options": {"ondelete": "CASCADE"},
    }
    unique_constraint: ReflectedUniqueConstraint = {
        "name": "uq_example_name",
        "column_names": ["name"],
    }
    check_constraint: ReflectedCheckConstraint = {
        "name": "ck_example_state",
        "sqltext": "state::text = 'ready'::text",
    }
    index: ReflectedIndex = {
        "name": "ix_example_name",
        "unique": False,
        "column_names": ["name"],
        "expressions": ["name"],
        "dialect_options": {"postgresql_where": "name IS NOT NULL"},
    }

    if mutation == "column.id.type":
        column["type"] = Integer()
    elif mutation == "column.id.nullable":
        column["nullable"] = True
    elif mutation == "column.id.server_default":
        column["default"] = "'mutated'::character varying"
    elif mutation == "primary_key.pk_example.columns":
        primary_key["constrained_columns"] = ["other_id"]
    elif mutation == "foreign_key.fk_example_parent.columns":
        foreign_key["constrained_columns"] = ["other_parent_id"]
    elif mutation == "foreign_key.fk_example_parent.referred_schema":
        foreign_key["referred_schema"] = "other_schema"
    elif mutation == "foreign_key.fk_example_parent.referred_table":
        foreign_key["referred_table"] = "other_parent"
    elif mutation == "foreign_key.fk_example_parent.referred_columns":
        foreign_key["referred_columns"] = ["other_id"]
    elif mutation == "foreign_key.fk_example_parent.ondelete":
        foreign_key["options"]["ondelete"] = "RESTRICT"
    elif mutation == "foreign_key.fk_example_parent.onupdate":
        foreign_key["options"]["onupdate"] = "CASCADE"
    elif mutation == "foreign_key.fk_example_parent.deferrable":
        foreign_key["options"]["deferrable"] = True
    elif mutation == "foreign_key.fk_example_parent.initially":
        foreign_key["options"]["initially"] = "DEFERRED"
    elif mutation == "foreign_key.fk_example_parent.match":
        foreign_key["options"]["match"] = "FULL"
    elif mutation == "unique_constraint.uq_example_name.columns":
        unique_constraint["column_names"] = ["other_name"]
    elif mutation == "check_constraint.ck_example_state.expression":
        check_constraint["sqltext"] = "state::text = 'mutated'::text"
    elif mutation == "index.ix_example_name.unique":
        index["unique"] = True
    elif mutation == "index.ix_example_name.columns":
        index["column_names"] = ["other_name"]
    elif mutation == "index.ix_example_name.expressions":
        index["expressions"] = ["lower(name::text)"]
    elif mutation == "index.ix_example_name.predicate":
        index["dialect_options"]["postgresql_where"] = "name IS NULL"
    elif mutation is not None:
        raise AssertionError(f"no reflected mutation for {mutation}")

    inspector = Mock(spec=Inspector)
    inspector.get_columns.return_value = [column]
    inspector.get_pk_constraint.return_value = primary_key
    inspector.get_foreign_keys.return_value = [foreign_key]
    inspector.get_unique_constraints.return_value = [unique_constraint]
    inspector.get_check_constraints.return_value = [check_constraint]
    inspector.get_indexes.return_value = [index]
    engine = create_engine("sqlite://")
    try:
        with engine.connect() as sync_conn:
            return _table_contract(
                sync_conn,
                cast(Inspector, inspector),
                "example",
                schema=None,
            )
    finally:
        engine.dispose()


def test_reflected_contract_records_every_compared_property() -> None:
    assert set(_reflected_table_contract()) == set(_COMPARED_PROPERTIES)


@pytest.mark.parametrize("property_name", _COMPARED_PROPERTIES)
def test_schema_diff_reports_each_mutated_property(property_name: str) -> None:
    metadata = _reflected_table_contract()
    migrated = _reflected_table_contract(mutation=property_name)

    differences = _schema_differences(
        metadata,
        migrated,
        prefix="table.example.",
    )
    message = _format_schema_differences(differences)

    assert [difference.property for difference in differences] == [
        f"table.example.{property_name}"
    ]
    assert f"table.example.{property_name}" in message
    assert "ORM metadata=" in message
    assert "migrated database=" in message


def test_schema_diff_reports_missing_property() -> None:
    differences = _schema_differences(
        {"column.id.type": "UUID"},
        {},
        prefix="table.example.",
    )

    assert _format_schema_differences(differences) == (
        "- table.example.column.id.type: ORM metadata='UUID'; "
        "migrated database=<absent>"
    )


def test_contract_diff_reports_unexpected_table() -> None:
    metadata = _SchemaContract(tables={})
    migrated = _SchemaContract(tables={"unexpected": {}})

    differences = _contract_differences(metadata, migrated)

    assert _format_schema_differences(differences) == (
        "- table.unexpected: ORM metadata=<absent>; migrated database='present'"
    )
