"""Audit operation codes (the ``operation`` column, DATABASE.md Section 9).

Centralised ``<resource>:<action>`` codes so recording points name one constant
instead of a stringly-typed literal. Most mirror the Appendix A permission codes
of the operation they record; the ``auth:*`` codes have no permission counterpart
(authentication is not permission-gated) but follow the same shape -- the
``operation`` column is free text, so this is a naming convention, not a catalog
constraint.
"""

from __future__ import annotations

from typing import Final

# Authentication (FR-AUTH-*): no permission counterpart; see module docstring.
AUTH_LOGIN: Final = "auth:login"
AUTH_LOGOUT: Final = "auth:logout"
AUTH_REGISTER: Final = "auth:register"
AUTH_REFRESH: Final = "auth:refresh"
# Reuse of an already-rotated refresh token: a security event distinct from a
# routine rotation, recorded as a DENIED outcome (token-theft / replay signal,
# SECURITY.md). Kept a separate code so the family-revocation trail is queryable
# apart from ordinary refresh activity.
AUTH_REFRESH_REUSE: Final = "auth:refresh_reuse"
# Account self-service (FR-AUTH self-service): the authenticated user changing
# their own password, profile (username/email), or deleting their account.
AUTH_PASSWORD_CHANGE: Final = "auth:password_change"
AUTH_PROFILE_UPDATE: Final = "auth:profile_update"
AUTH_ACCOUNT_DELETE: Final = "auth:account_delete"

# Community provisioning/management (FR-COMM-*).
COMMUNITY_PROVISION: Final = "community:provision"
COMMUNITY_UPDATE: Final = "community:update"
COMMUNITY_DELETE: Final = "community:delete"

# Membership + role assignment (FR-MEM-*).
MEMBER_ADD: Final = "member:add"
MEMBER_REMOVE: Final = "member:remove"
ROLE_ASSIGN: Final = "role:assign"
ROLE_UNASSIGN: Final = "role:unassign"

# Role / grant CRUD (FR-AUTHZ-*).
ROLE_CREATE: Final = "role:create"
ROLE_UPDATE: Final = "role:update"
ROLE_DELETE: Final = "role:delete"
GRANT_CREATE: Final = "grant:create"
GRANT_REVOKE: Final = "grant:revoke"

# Server CRUD + lifecycle + RCON (FR-SRV-*).
SERVER_CREATE: Final = "server:create"
SERVER_UPDATE: Final = "server:update"
SERVER_DELETE: Final = "server:delete"
SERVER_START: Final = "server:start"
SERVER_STOP: Final = "server:stop"
SERVER_RESTART: Final = "server:restart"
SERVER_COMMAND: Final = "server:command"

# Backup create/restore/delete (FR-BAK-*).
BACKUP_CREATE: Final = "backup:create"
BACKUP_RESTORE: Final = "backup:restore"
BACKUP_DELETE: Final = "backup:delete"

# File upload / download (FR-FILE-*, issue #259). Recorded under the file:edit /
# file:read permissions they require.
FILE_UPLOAD: Final = "file:upload"
FILE_DOWNLOAD: Final = "file:download"

# Worker drain set/clear (FR-WRK-5).
WORKER_DRAIN_SET: Final = "worker:drain_set"
WORKER_DRAIN_CLEAR: Final = "worker:drain_clear"

# Target-type names (the ``target_type`` column).
TARGET_COMMUNITY: Final = "community"
TARGET_USER: Final = "user"
TARGET_ROLE: Final = "role"
TARGET_GRANT: Final = "grant"
TARGET_SERVER: Final = "server"
TARGET_BACKUP: Final = "backup"
TARGET_WORKER: Final = "worker"
TARGET_FILE: Final = "file"
