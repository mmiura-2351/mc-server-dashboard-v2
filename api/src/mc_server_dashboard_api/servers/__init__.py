"""Servers context: Minecraft server records, persistence, and CRUD endpoints.

Holds the servers bounded context's quadrants (domain / application / adapters /
api). A :class:`~.domain.entities.Server` is community-scoped (FR-SRV-3) and
carries the desired/observed state split (FR-SRV-4). This sub-issue lands the
entity, its persistence, and the CRUD endpoints; lifecycle commands
(start/stop/restart/RCON), Worker interaction, JAR resolution, and hydrate land
with their own features (issue #7 epic).
"""
