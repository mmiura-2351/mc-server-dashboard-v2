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

# Platform-admin user administration (M2 Epic A2, issue #278): an admin acting on
# another user's lifecycle. These name the operation, not a permission code --
# the admin axis is gated by the platform-admin flag, not a catalog permission --
# but follow the same ``<resource>:<action>`` shape; the actor is the admin and
# the target is the affected user.
USER_DEACTIVATE: Final = "user:deactivate"
USER_REACTIVATE: Final = "user:reactivate"
USER_DELETE: Final = "user:delete"
USER_PLATFORM_ADMIN_GRANT: Final = "user:platform_admin_grant"
USER_PLATFORM_ADMIN_REVOKE: Final = "user:platform_admin_revoke"

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
# Whole-server ZIP export / import (M2 Epic C2, issue #274). These name the
# operation, not a permission code: export is gated by file:read (bulk file read)
# and import by server:create; there is no server:export / server:import
# permission in the catalog. The ``operation`` column is free text (see module
# docstring), so a recording-only code is fine.
SERVER_EXPORT: Final = "server:export"
SERVER_IMPORT: Final = "server:import"

# Backup create/restore/delete + transfer (FR-BAK-*). Upload/download (issue #281)
# name the operation, not a permission: upload is gated by backup:create and
# download by backup:read; there is no backup:upload / backup:download permission
# in the catalog. The ``operation`` column is free text (see module docstring).
BACKUP_CREATE: Final = "backup:create"
BACKUP_RESTORE: Final = "backup:restore"
BACKUP_DELETE: Final = "backup:delete"
BACKUP_UPLOAD: Final = "backup:upload"
BACKUP_DOWNLOAD: Final = "backup:download"

# File upload / download / rename / delete / mkdir / search (FR-FILE-*, issue
# #259) plus write / rollback (issue #263). Recorded under the file:edit /
# file:read permissions they require.
#
# Audit rule for file routes (issue #263): MUTATIONS are audited (write,
# rollback, rename, delete, mkdir, upload), and so are bulk / exfiltration-shaped
# READS (download) -- but granular reads (read a single file, list a directory,
# list a file's versions) are NOT, as they are high-volume and low-signal. This
# matches the backup posture: backup create/restore/delete/upload/download are
# audited while the granular backup:read routes (list, statistics) are not.
FILE_WRITE: Final = "file:write"
FILE_ROLLBACK: Final = "file:rollback"
FILE_UPLOAD: Final = "file:upload"
FILE_DOWNLOAD: Final = "file:download"
FILE_RENAME: Final = "file:rename"
FILE_DELETE: Final = "file:delete"
FILE_MKDIR: Final = "file:mkdir"
FILE_SEARCH: Final = "file:search"

# Version catalog admin (M2 Epic D5, issue #286): a platform admin manually
# refreshing the in-process catalog cache. Names the operation, not a permission
# code -- the admin axis is gated by the platform-admin flag, not a catalog
# permission. Platform-level (no community/target resource).
VERSION_REFRESH: Final = "version:refresh"

# JAR-pool garbage collection (M2 Epic D4, issue #293): a platform admin manually
# triggering the reference-counted pool sweep. Same platform-level posture as
# version:refresh -- an operation name, not a permission code.
VERSION_JAR_GC: Final = "version:jar_gc"

# Worker drain set/clear (FR-WRK-5).
WORKER_DRAIN_SET: Final = "worker:drain_set"
WORKER_DRAIN_CLEAR: Final = "worker:drain_clear"

# Player-group CRUD / player edits / attach-detach (issue #276). Recorded under
# the group:manage permission the mutating routes require.
GROUP_CREATE: Final = "group:create"
GROUP_UPDATE: Final = "group:update"
GROUP_DELETE: Final = "group:delete"
GROUP_PLAYER_ADD: Final = "group:player_add"
GROUP_PLAYER_REMOVE: Final = "group:player_remove"
GROUP_ATTACH: Final = "group:attach"
GROUP_DETACH: Final = "group:detach"

# Target-type names (the ``target_type`` column).
TARGET_COMMUNITY: Final = "community"
TARGET_USER: Final = "user"
TARGET_ROLE: Final = "role"
TARGET_GRANT: Final = "grant"
TARGET_SERVER: Final = "server"
TARGET_BACKUP: Final = "backup"
TARGET_WORKER: Final = "worker"
TARGET_FILE: Final = "file"
TARGET_GROUP: Final = "group"
