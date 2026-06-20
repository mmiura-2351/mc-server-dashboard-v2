"""Shared reads over a plugin's stored Modrinth catalog dependencies (#1321).

A Modrinth-sourced plugin captures its selected version's **required** catalog
dependencies at ingest, keyed by ``project_id`` (a different namespace from the
manifest ``mod_identifier`` deps). The persisted shape is
``[{"project_id": str, "required": bool, "slug": str | None, "title": str |
None}]``. Both phase-B validation and phase-C resolution read these the same way,
so the parsing lives here once.

A catalog dep is satisfied iff some installed plugin has a matching
``source_project_id`` (Modrinth ↔ Modrinth, by id). Only a Modrinth-sourced
plugin's catalog deps are evaluated; a local upload has none.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.plugin import PluginSource, ServerPlugin


@dataclass(frozen=True)
class CatalogDep:
    """One required Modrinth catalog dependency, keyed by ``project_id``."""

    project_id: str
    slug: str | None
    title: str | None


def required_catalog_deps(plugin: ServerPlugin) -> list[CatalogDep]:
    """The plugin's required catalog deps (empty for a non-Modrinth plugin).

    Reads the persisted ``catalog_dependencies`` list, keeping only entries
    flagged ``required`` with a non-empty ``project_id``. Tolerant of a malformed
    entry: a non-dict or one without a string ``project_id`` is skipped.
    """

    if plugin.source is not PluginSource.MODRINTH:
        return []
    deps: list[CatalogDep] = []
    for raw in plugin.catalog_dependencies:
        if not isinstance(raw, dict) or not raw.get("required"):
            continue
        project_id = raw.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            continue
        slug = raw.get("slug")
        title = raw.get("title")
        deps.append(
            CatalogDep(
                project_id=project_id,
                slug=slug if isinstance(slug, str) else None,
                title=title if isinstance(title, str) else None,
            )
        )
    return deps


def installed_project_ids(plugins: list[ServerPlugin]) -> set[str]:
    """Every Modrinth ``source_project_id`` present in the installed set."""

    return {p.source_project_id for p in plugins if p.source_project_id is not None}
