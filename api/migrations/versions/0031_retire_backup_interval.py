"""retire FR-BAK-3 backup_interval_hours into a backup schedule

Data-only cutover (epic #649, issue #1840). The legacy FR-BAK-3 per-server
backup cadence rode the ``backup_interval_hours`` reserved key on
``server.config``; it is now a first-class ``backup`` schedule (the ``schedule``
table, created by 0029). This migration converts each server that carries the
key into an equivalent enabled interval ``backup`` schedule and strips the key,
so exactly one cadence runner (the general scheduler, #1838) drives backups.

**Upgrade.** For every server whose config holds ``backup_interval_hours``:

- validate the historical value first: only a JSON *number* that is a whole
  number of hours ``>= 1`` (and small enough that ``hours * 3600`` fits the
  ``interval_seconds`` int4 column) is convertible. The legacy CREATE path never
  validated this key (only PATCH did), so a zero/negative/non-integer value can
  exist; converting one would produce a schedule row the runner's domain
  hydration rejects on every tick. An invalid value is **stripped without
  creating a schedule** and WARN-logged with the server id and the bad value —
  a skipped row beats an aborted deploy;
- for a valid value, insert a ``schedule`` row: ``action = 'backup'``,
  ``interval_seconds = hours * 3600`` (always >= 3600, comfortably above the
  domain's 60 s interval floor), ``enabled = true``, an empty ``{}`` payload,
  ``timezone = 'UTC'``, ``cron = NULL``. The name is ``"Scheduled backup"``, or
  ``"Scheduled backup N"`` on conflict, so the ``UNIQUE(server_id, name)``
  constraint is never violated by a schedule the operator already created via
  the CRUD API (#1846);
- set ``next_run_at = now() + offset`` where ``offset`` is a per-row
  Python-computed ``random() * min(interval_seconds, 3600)`` seconds. An
  enabled schedule with ``next_run_at`` NULL is never picked up by the runner's
  due query, so it MUST be set. The offset makes each migrated schedule become
  due within roughly one hour of the migration (not after a full multi-day
  interval), while the per-row randomness staggers a fleet so they do not all
  fire at the same instant — mirroring the FR-BAK-3 precedent of re-backing-up
  each server shortly after an API restart within a jitter window. The offset
  is computed in Python and bound as a single float parameter: SQL-side
  ``random() * least(:param, ...) * interval`` arithmetic breaks under
  asyncpg's parameter typing (untyped parameters degrade to text and the
  ``double precision * text`` multiply has no operator).

- strip ``backup_interval_hours`` from the config blob.

**Behavior change (documented, breaking).** A failed scheduled backup no longer
retries every scheduler tick. It now gets the runner's bounded retry (one, ~30
minutes later) plus an operator notification, then waits for the next
occurrence (schedule_runner.py).

**Downgrade (best-effort, lossy).** Reconstructs ``backup_interval_hours`` from
every enabled interval ``backup`` schedule whose interval is a whole number of
hours and deletes that schedule. This cannot perfectly reverse the cutover: it
also reverts whole-hour interval ``backup`` schedules created directly via the
CRUD API (indistinguishable from a migrated one), it cannot represent a sub-hour
interval as an integer-hours key (such schedules are left in place), it does not
resurrect an invalid value the upgrade stripped, and it drops schedule-only
attributes (name, jitter, run history). A server carrying several such schedules
keeps only the last one's interval in the restored key.

Revision ID: 0031_retire_backup_interval
Revises: 0030_schedule_permissions
Create Date: 2026-07-12
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0031_retire_backup_interval"
down_revision: str | None = "0030_schedule_permissions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LOG = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_BASE_NAME = "Scheduled backup"
# ``interval_seconds`` is int4; a converted value must fit or the INSERT aborts
# the whole migration. 596523 hours (~68 years) — anything above is a typo, and
# it is skipped-and-logged like the other invalid shapes.
_MAX_HOURS = (2**31 - 1) // _SECONDS_PER_HOUR

_INSERT_SCHEDULE = sa.text(
    "INSERT INTO schedule (id, server_id, name, action, payload, cron, "
    "interval_seconds, timezone, enabled, next_run_at, last_run_at, created_by, "
    "created_at, updated_at) VALUES "
    "(:id, :server_id, :name, 'backup', '{}'::jsonb, NULL, :interval_seconds, "
    "'UTC', true, "
    "now() + make_interval(secs => CAST(:offset_seconds AS double precision)), "
    "NULL, NULL, now(), now())"
)


def _valid_hours(raw: str | None, typ: str | None) -> int | None:
    """The value as a positive whole number of hours, or ``None`` if invalid.

    The legacy CREATE path never validated ``backup_interval_hours`` (only PATCH
    did), so a stored value can be any JSON type or number. Convertible means: a
    JSON number, integral, ``>= 1``, and small enough that ``hours * 3600`` fits
    the int4 ``interval_seconds`` column. Everything else returns ``None`` and
    the caller strips the key without creating a schedule.
    """

    if typ != "number" or raw is None:
        return None
    value = Decimal(raw)
    if value < 1 or value > _MAX_HOURS or value != value.to_integral_value():
        return None
    return int(value)


def _unique_name(conn: sa.engine.Connection, server_id: uuid.UUID) -> str:
    """A ``"Scheduled backup"`` name that does not collide for this server.

    Falls back to ``"Scheduled backup 2"``, ``3``, ... so the migration never
    trips ``UNIQUE(server_id, name)`` against a schedule the operator already
    created via the CRUD API (#1846).
    """

    existing = {
        row[0]
        for row in conn.execute(
            sa.text("SELECT name FROM schedule WHERE server_id = :sid"),
            {"sid": server_id},
        )
    }
    if _BASE_NAME not in existing:
        return _BASE_NAME
    suffix = 2
    while f"{_BASE_NAME} {suffix}" in existing:
        suffix += 1
    return f"{_BASE_NAME} {suffix}"


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, config->>'backup_interval_hours' AS raw, "
            "jsonb_typeof(config->'backup_interval_hours') AS typ "
            "FROM server WHERE jsonb_exists(config, 'backup_interval_hours') "
            "ORDER BY created_at"
        )
    ).fetchall()
    for server_id, raw, typ in rows:
        hours = _valid_hours(raw, typ)
        if hours is None:
            _LOG.warning(
                "server %s carried an invalid backup_interval_hours value %r; "
                "stripping the key WITHOUT creating a backup schedule — "
                "recreate the cadence as a schedule manually if it was wanted",
                server_id,
                raw,
            )
        else:
            interval_seconds = hours * _SECONDS_PER_HOUR
            offset_seconds = random.random() * min(  # noqa: S311 - jitter, not crypto
                interval_seconds, _SECONDS_PER_HOUR
            )
            conn.execute(
                _INSERT_SCHEDULE,
                {
                    "id": uuid.uuid4(),
                    "server_id": server_id,
                    "name": _unique_name(conn, server_id),
                    "interval_seconds": interval_seconds,
                    "offset_seconds": offset_seconds,
                },
            )
        conn.execute(
            sa.text(
                "UPDATE server SET config = config - 'backup_interval_hours', "
                "updated_at = now() WHERE id = :id"
            ),
            {"id": server_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, server_id, interval_seconds FROM schedule "
            "WHERE action = 'backup' AND enabled = true AND cron IS NULL "
            "AND mod(interval_seconds, :hour) = 0 "
            "ORDER BY created_at"
        ),
        {"hour": _SECONDS_PER_HOUR},
    ).fetchall()
    for schedule_id, server_id, interval_seconds in rows:
        conn.execute(
            sa.text(
                "UPDATE server SET config = jsonb_set("
                "config, '{backup_interval_hours}', "
                "to_jsonb(CAST(:hours AS integer))), "
                "updated_at = now() WHERE id = :sid"
            ),
            {"hours": interval_seconds // _SECONDS_PER_HOUR, "sid": server_id},
        )
        conn.execute(
            sa.text("DELETE FROM schedule WHERE id = :id"),
            {"id": schedule_id},
        )
