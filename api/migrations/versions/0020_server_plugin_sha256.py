"""server plugin sha256: content-addressed cache column + index

Adds the ``sha256`` column to ``server_plugin`` (issue #1306): the SHA-256
content address of the installed jar in the content-addressed cache. Identical
content shares one cached blob keyed by this hash; ``checksum_sha512`` stays for
Modrinth integrity verification. Nullable so existing rows (installed before the
cache) keep a NULL content address and are simply not cache-deduped.

Also indexes ``checksum_sha512``: the download-cache lookup maps a Modrinth
version's published SHA-512 to the cached SHA-256, so the same version is not
re-downloaded per server.

Revision ID: 0020_server_plugin_sha256
Revises: 0019_server_plugins
Create Date: 2026-06-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_server_plugin_sha256"
down_revision: str | None = "0019_server_plugins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "server_plugin",
        sa.Column("sha256", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_server_plugin_checksum_sha512",
        "server_plugin",
        ["checksum_sha512"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_server_plugin_checksum_sha512",
        table_name="server_plugin",
    )
    op.drop_column("server_plugin", "sha256")
