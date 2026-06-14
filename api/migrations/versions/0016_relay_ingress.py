"""relay ingress: server.slug

Adds the ``slug`` column to the ``server`` table (RELAY.md Section 3, issue #955).
The slug is a deployment-wide unique DNS label used as the relay hostname prefix
(``<slug>.<base_domain>``). It is:

- ``TEXT NOT NULL`` with a deployment-wide ``UNIQUE`` constraint (``uq_server_slug``).
- **Backfilled** for pre-existing rows during this migration using the same
  ``<word>-<word>-<NN>`` wordlist as the application's slug generator. The
  backfill runs inside the migration transaction, so if it fails the migration
  is rolled back and no rows are left with an empty slug.

The column is added as nullable first, then backfilled, then set NOT NULL and the
UNIQUE constraint added — the standard Postgres migration pattern for backfilling
a NOT NULL column on an existing table.

Downgrade drops the constraint and the column.

Revision ID: 0016_relay_ingress
Revises: 0015_backup_health
Create Date: 2026-06-12
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_relay_ingress"
down_revision: str | None = "0015_backup_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Wordlist mirrors slug.py — kept inline so the migration is self-contained.
_WORDS_A = (
    "amber",
    "arctic",
    "azure",
    "bold",
    "bright",
    "calm",
    "cedar",
    "clear",
    "coral",
    "crisp",
    "cyan",
    "dark",
    "dawn",
    "deep",
    "dusk",
    "elder",
    "ember",
    "fern",
    "flame",
    "flint",
    "frost",
    "golden",
    "green",
    "grey",
    "indigo",
    "iron",
    "jade",
    "keen",
    "lake",
    "lark",
    "lemon",
    "light",
    "lime",
    "lunar",
    "maple",
    "marsh",
    "mist",
    "moss",
    "night",
    "north",
    "oak",
    "ocean",
    "olive",
    "opal",
    "pine",
    "plain",
    "polar",
    "rapid",
    "reef",
    "rose",
    "ruby",
    "sage",
    "sand",
    "sea",
    "sharp",
    "silent",
    "silver",
    "sky",
    "slate",
    "snow",
    "solar",
    "south",
    "stark",
    "star",
    "steel",
    "stone",
    "storm",
    "sunny",
    "swift",
    "teal",
    "terra",
    "tide",
    "timber",
    "true",
    "vale",
    "verdant",
    "violet",
    "vivid",
    "wave",
    "west",
    "wild",
    "wind",
    "winter",
    "wood",
)
_WORDS_B = (
    "ant",
    "asp",
    "bass",
    "bear",
    "bee",
    "bison",
    "boar",
    "buck",
    "bull",
    "carp",
    "cat",
    "clam",
    "cod",
    "condor",
    "crab",
    "crane",
    "crow",
    "cub",
    "dart",
    "deer",
    "doe",
    "dove",
    "duck",
    "eagle",
    "eel",
    "elk",
    "falcon",
    "finch",
    "fish",
    "flea",
    "fly",
    "fox",
    "frog",
    "gull",
    "hare",
    "hawk",
    "heron",
    "ibis",
    "jay",
    "kite",
    "lamb",
    "lark",
    "lion",
    "lynx",
    "mink",
    "mole",
    "moth",
    "mule",
    "newt",
    "owl",
    "perch",
    "pike",
    "plover",
    "pony",
    "puma",
    "quail",
    "ram",
    "raven",
    "ray",
    "robin",
    "rook",
    "seal",
    "shark",
    "shrew",
    "slug",
    "snipe",
    "sparrow",
    "stag",
    "starling",
    "stoat",
    "stork",
    "swift",
    "toad",
    "trout",
    "viper",
    "vole",
    "wasp",
    "weasel",
    "whale",
    "wren",
    "yak",
    "zebra",
)


def _backfill_slugs(conn: sa.engine.Connection) -> None:
    """Generate and assign a unique slug for every existing server row."""

    rows = conn.execute(sa.text("SELECT id FROM server ORDER BY created_at")).fetchall()
    used: set[str] = set()
    for (server_id,) in rows:
        for _ in range(200):
            candidate = (
                f"{random.choice(_WORDS_A)}"  # noqa: S311
                f"-{random.choice(_WORDS_B)}"  # noqa: S311
                f"-{random.randint(0, 99):02d}"  # noqa: S311
            )
            if candidate not in used:
                used.add(candidate)
                conn.execute(
                    sa.text("UPDATE server SET slug = :slug WHERE id = :id"),
                    {"slug": candidate, "id": server_id},
                )
                break
        else:
            raise RuntimeError(
                f"could not generate a unique slug for server {server_id} "
                "after 200 attempts"
            )


def upgrade() -> None:
    # 1. Add slug as nullable so existing rows can be backfilled.
    op.add_column(
        "server",
        sa.Column("slug", sa.String(), nullable=True),
    )
    # 2. Backfill existing rows.
    bind = op.get_bind()
    _backfill_slugs(bind)
    # 3. Enforce NOT NULL now that every row has a slug.
    op.alter_column("server", "slug", nullable=False)
    # 4. Add the deployment-wide UNIQUE constraint.
    op.create_unique_constraint("uq_server_slug", "server", ["slug"])


def downgrade() -> None:
    op.drop_constraint("uq_server_slug", "server", type_="unique")
    op.drop_column("server", "slug")
