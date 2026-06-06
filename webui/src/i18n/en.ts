// English dictionary (shipped). A second locale (e.g. Japanese) can be added as
// a sibling object with the same keys; see WEBUI_SPEC.md Section 7.5.
export const en = {
  "app.title": "mc-server-dashboard",

  // Shell chrome
  "shell.brand": "MC Dashboard",
  "shell.account": "Account",
  "shell.switchCommunity": "Switch community",
  "shell.noCommunity": "No community",
  "shell.noCommunities": "You are not a member of any community yet.",

  // Sidebar navigation (WEBUI_SPEC.md Section 5)
  "nav.community": "Community",
  "nav.dashboard": "Dashboard",
  "nav.createServer": "Create server",
  "nav.settings": "Community settings",
  "nav.admin": "Platform admin",
  "nav.adminOverview": "Overview",
  "nav.adminUsers": "Users",
  "nav.adminCommunities": "Communities",
  "nav.adminWorkers": "Workers",
  "nav.adminVersions": "Versions & JARs",
  "nav.adminAudit": "Global audit",

  // Placeholder pages (Phase 1: routing skeleton only)
  "page.login": "Sign in",
  "page.register": "Register",
  "page.dashboard": "Servers",
  "page.serverCreate": "Create server",
  "page.serverDetail": "Server detail",
  "page.communitySettings": "Community settings",
  "page.account": "Account",
  "page.adminOverview": "Platform overview",
  "page.adminUsers": "User management",
  "page.adminCommunities": "Communities",
  "page.adminWorkers": "Workers",
  "page.adminVersions": "Versions & JARs",
  "page.adminAudit": "Global audit log",
  "page.placeholder": "Placeholder page — content arrives in a later phase.",
  "page.notFound": "Page not found",

  // Auth links
  "auth.toRegister": "No account? Register",
  "auth.toLogin": "Have an account? Sign in",

  // Login / register pages and route guards (issue #410). Kept as one
  // contiguous block to minimize merge conflicts with sibling i18n PRs.
  "auth.loading": "Loading…",
  "auth.fieldUsername": "Username",
  "auth.fieldEmail": "Email",
  "auth.fieldPassword": "Password",
  "login.usernamePlaceholder": "username",
  "login.passwordPlaceholder": "••••••••",
  "login.submit": "Sign in",
  "login.submitting": "Signing in…",
  "login.invalidCredentials": "Invalid username or password.",
  "login.genericError": "Could not sign in. Please try again.",
  "register.usernamePlaceholder": "username",
  "register.emailPlaceholder": "you@example.com",
  "register.passwordPlaceholder": "min. 12 characters",
  "register.confirmPassword": "Confirm password",
  "register.passwordHint":
    "At least 12 characters. Must not contain your username or email. Common passwords are rejected.",
  "register.submit": "Create account",
  "register.submitting": "Creating account…",
  "register.success": "Account created. Please sign in.",
  "register.genericError": "Could not create your account. Please try again.",
  "register.errPasswordMismatch": "Passwords do not match.",
  // Server-authoritative reason codes (AUTH_API.md 2; users.py register).
  "register.reason.too_short": "Password is too short.",
  "register.reason.too_long_for_bcrypt": "Password is too long.",
  "register.reason.insufficient_complexity":
    "Password is not complex enough; use a longer or more varied password.",
  "register.reason.common_password": "Password is too common.",
  "register.reason.contains_user_info":
    "Password must not contain your username or email.",
  "register.reason.simple_pattern":
    "Password is too simple or a common pattern.",
  "register.reason.username_taken": "That username is already taken.",
  "register.reason.email_taken": "That email is already registered.",
  "register.reason.invalid_username": "That username is not allowed.",
  "register.reason.invalid_email": "Enter a valid email address.",

  // UX primitives (WEBUI_SPEC.md Section 7.4)
  "common.cancel": "Cancel",
  "common.close": "Close",

  // Account page (WEBUI_SPEC.md 6.11) — one contiguous block to minimise
  // merge conflicts with sibling PRs adding keys under other prefixes.
  "account.subtitle": "Your profile, security, and memberships.",
  "account.signOut": "Sign out",
  "account.loading": "Loading…",
  "account.loadError": "Could not load your account. Try refreshing.",

  "account.profile.heading": "Profile",
  "account.profile.username": "Username",
  "account.profile.email": "Email",
  "account.profile.save": "Save profile",
  "account.profile.saved": "Profile updated.",

  "account.password.heading": "Password",
  "account.password.current": "Current password",
  "account.password.new": "New password",
  "account.password.confirm": "Confirm new password",
  "account.password.change": "Change password",
  "account.password.changed": "Password changed.",
  "account.password.mismatch": "The new passwords do not match.",
  "account.password.hint":
    "At least 12 characters. Avoid your username, email, or simple patterns.",

  "account.memberships.heading": "Memberships",
  "account.memberships.community": "Community",
  "account.memberships.none": "You are not a member of any community yet.",
  "account.memberships.loadError":
    "Could not load your memberships. Try refreshing.",

  "account.delete.heading": "Danger zone",
  "account.delete.label": "Delete account",
  "account.delete.desc":
    "Removes your account and all memberships. Communities you own must be transferred or deleted first.",
  "account.delete.open": "Delete…",
  "account.delete.dialogTitle": "Delete your account",
  "account.delete.dialogBody":
    "This permanently deletes your account. This cannot be undone. Type your username to confirm.",
  "account.delete.confirm": "Delete account",
  "account.delete.prompt": "Type your username to enable deletion",

  // API reason codes (RFC 9457 `reason`) surfaced inline / via toast.
  "account.error.username_taken": "That username is already taken.",
  "account.error.email_taken": "That email is already in use.",
  "account.error.invalid_username": "That username is not valid.",
  "account.error.invalid_email": "That email is not valid.",
  "account.error.invalid_credentials": "Your current password is incorrect.",
  "account.error.too_short": "Password is too short.",
  "account.error.too_long": "Password is too long.",
  "account.error.too_long_for_bcrypt": "Password is too long.",
  "account.error.insufficient_complexity":
    "Password is too simple — mix character types or make it longer.",
  "account.error.common_password": "That password is too common.",
  "account.error.contains_user_info":
    "Password must not contain your username or email.",
  "account.error.simple_pattern":
    "Avoid repeated characters or sequential runs.",
  "account.error.owns_community":
    "Transfer or delete the communities you own before deleting your account.",
  "account.error.last_platform_admin":
    "You are the last platform admin and cannot delete your account.",
  "account.error.generic": "Something went wrong. Please try again.",

  // Dashboard server cards (WEBUI_SPEC.md 6.2). One contiguous block to keep
  // merge conflicts with sibling i18n PRs minimal.
  "dashboard.subtitle": "Servers in this community.",
  "dashboard.loading": "Loading servers…",
  "dashboard.loadError": "Could not load servers. Try refreshing.",
  "dashboard.empty": "No servers yet.",
  "dashboard.emptyHint": "Create your first server to get started.",
  "dashboard.createServer": "Create server",
  "dashboard.noWorker": "no worker assigned",
  "dashboard.start": "Start",
  "dashboard.stop": "Stop",
  "dashboard.restart": "Restart",
  // Observed-state pill labels (WEBUI_SPEC.md 2.3).
  "dashboard.state.starting": "starting",
  "dashboard.state.running": "running",
  "dashboard.state.stopping": "stopping",
  "dashboard.state.stopped": "stopped",
  "dashboard.state.restarting": "restarting",
  "dashboard.state.crashed": "crashed",
  "dashboard.state.unknown": "unknown",
  // Lifecycle action feedback.
  "dashboard.actionFailed": "Could not complete that action. Please try again.",
  // Conflict-flavoured (server_unsettled-style) lifecycle races (SPEC 7.4).
  "dashboard.stateChanged": "State changed — refreshed.",
  // Sanitized 409 start-failure reasons (issue #225); specific, actionable
  // causes rather than the generic state-changed toast.
  "dashboard.lifecycle.portConflict":
    "Could not start: a port is already in use.",
  "dashboard.lifecycle.imageMissing":
    "Could not start: the server image is missing.",
  // Live-status degraded indicator: WS down, polling fallback (SPEC 6.2 / 7.2).
  "dashboard.liveDegraded": "Live updates degraded — polling",

  // Server detail page (WEBUI_SPEC.md 6.4 / 6.9). One contiguous block to keep
  // merge conflicts with sibling i18n PRs minimal.
  "serverDetail.loading": "Loading server…",
  "serverDetail.loadError": "Could not load this server. Try refreshing.",
  "serverDetail.breadcrumb": "Servers",
  // Overview header.
  "serverDetail.converging": "settling…",
  "serverDetail.desired": "desired",
  "serverDetail.observed": "observed",
  "serverDetail.noWorker": "no worker assigned",
  "serverDetail.noPort": "no port",
  // Tabs (WEBUI_SPEC.md 6.4–6.9). Built tabs: Overview, Settings; the rest are
  // placeholders pending later phases.
  "serverDetail.tab.overview": "Overview",
  "serverDetail.tab.console": "Console",
  "serverDetail.tab.files": "Files",
  "serverDetail.tab.backups": "Backups",
  "serverDetail.tab.players": "Players",
  "serverDetail.tab.settings": "Settings",
  "serverDetail.tabPlaceholder": "Coming in a later phase.",
  // Overview live metrics strip + log tail (issue #440, WEBUI_SPEC.md 6.4).
  "serverDetail.metric.cpu": "CPU",
  "serverDetail.metric.memory": "Memory",
  "serverDetail.metric.players": "Players",
  "serverDetail.metric.cores": "cores",
  "serverDetail.metric.mib": "MiB",
  "serverDetail.logTailHeading": "Recent log",
  "serverDetail.openConsole": "Open Console",
  "serverDetail.logTailEmpty": "No log output yet.",
  // Inline divider where the client fell behind and missed events (SPEC 7.2).
  "serverDetail.missedEvents": "— missed events —",
  // Console tab (issue #440, WEBUI_SPEC.md 6.5).
  "serverDetail.console.follow": "Follow",
  "serverDetail.console.filter": "Filter…",
  "serverDetail.console.clear": "Clear",
  "serverDetail.console.send": "Send",
  "serverDetail.console.commandPlaceholder": "Type a command…",
  "serverDetail.console.notRunning":
    "Commands are available only while the server is running.",
  "serverDetail.commandFailed": "Command failed.",
  // Lifecycle controls.
  "serverDetail.start": "Start",
  "serverDetail.stop": "Stop",
  "serverDetail.stopGraceful": "Stop (graceful)",
  "serverDetail.stopForce": "Force stop",
  "serverDetail.restart": "Restart",
  "serverDetail.export": "Export",
  "serverDetail.delete": "Delete",
  // Settings tab (WEBUI_SPEC.md 6.9).
  "serverDetail.settings.general": "General",
  "serverDetail.settings.name": "Server name",
  "serverDetail.settings.gamePort": "Game port",
  "serverDetail.settings.executionBackend": "Execution backend",
  "serverDetail.settings.executionBackendHint": "Immutable after creation.",
  "serverDetail.settings.config": "Config overrides",
  "serverDetail.settings.configKey": "Key",
  "serverDetail.settings.configValue": "Value",
  "serverDetail.settings.configAdd": "Add override",
  "serverDetail.settings.configRemove": "Remove",
  "serverDetail.settings.configHint":
    "Values are read as JSON: 12 is a number, true a boolean, anything else text.",
  "serverDetail.settings.save": "Save changes",
  "serverDetail.settings.saved": "Settings saved.",
  "serverDetail.settings.atRestHint":
    "Name, game port and config changes need the server stopped.",
  // On-blur game-port availability check (GET /ports/check/{port}).
  "serverDetail.port.available": "✓ available",
  "serverDetail.port.current": "✓ available (current)",
  "serverDetail.port.taken": "Port is already in use.",
  "serverDetail.port.outOfRange": "Port is outside the allowed range.",
  "serverDetail.port.checkError": "Could not check port availability.",
  // Danger zone.
  "serverDetail.danger.heading": "Danger zone",
  "serverDetail.danger.exportTitle": "Export server",
  "serverDetail.danger.exportDesc":
    "Download the full working set as a ZIP archive.",
  "serverDetail.danger.exportButton": "Export ZIP",
  "serverDetail.danger.deleteTitle": "Delete server",
  "serverDetail.danger.deleteDesc":
    "Permanently removes the server, its data and backups.",
  "serverDetail.danger.deleteButton": "Delete…",
  "serverDetail.delete.dialogTitle": "Delete server",
  "serverDetail.delete.dialogBody":
    "This permanently deletes the server, its data and backups. This cannot be undone. Type the server name to confirm.",
  "serverDetail.delete.confirm": "Delete server",
  "serverDetail.delete.prompt": "Type the server name to enable deletion",
  // Outcomes (toasts). 409/422 reasons surfaced specifically; otherwise generic.
  "serverDetail.exportStarted": "Export download started.",
  "serverDetail.deleted": "Server deleted.",
  "serverDetail.error.notStopped": "Stop the server before making this change.",
  "serverDetail.error.unsettled":
    "The server must be stopped before exporting.",
  "serverDetail.error.portTaken": "That game port is already in use.",
  "serverDetail.error.portOutOfRange":
    "That game port is outside the allowed range.",
  "serverDetail.error.invalidSnapshotInterval":
    "snapshot_interval_seconds must be a whole number of seconds at or above the configured floor.",
  "serverDetail.error.invalidBackupSchedule":
    "backup_interval_hours must be a whole number of hours of at least 1.",
  "serverDetail.error.generic": "Something went wrong. Please try again.",

  // Backups tab (WEBUI_SPEC.md 6.7). One contiguous block to keep merge
  // conflicts with sibling i18n PRs minimal.
  "backups.loading": "Loading backups…",
  "backups.loadError": "Could not load backups.",
  "backups.noRead": "You do not have permission to view backups.",
  "backups.none": "—",
  "backups.empty": "No backups yet.",
  // Stats header.
  "backups.stat.count": "Backups",
  "backups.stat.totalSize": "Total size",
  "backups.stat.newest": "Newest",
  "backups.stat.oldest": "Oldest",
  // Table.
  "backups.col.created": "Created",
  "backups.col.source": "Source",
  "backups.col.size": "Size",
  "backups.col.creator": "By",
  "backups.unknownSize": "unknown",
  "backups.unknownCreator": "—",
  // Actions.
  "backups.create": "+ Create backup",
  "backups.upload": "Upload",
  "backups.download": "Download",
  "backups.restore": "Restore",
  "backups.delete": "Delete",
  // Schedule field (backup_interval_hours on the server config blob).
  "backups.schedule.label": "Schedule: every",
  "backups.schedule.unit": "hours",
  "backups.schedule.save": "Save",
  "backups.schedule.saved": "Backup schedule saved.",
  // Restore dialog (stopped-only; two-step stop-then-restore).
  "backups.restoreDialog.title": "Restore backup",
  "backups.restoreDialog.blocked":
    "Restoring overwrites the server's data and requires the server to be stopped.",
  "backups.restoreDialog.blockedHint":
    "Stop the server, then reopen this dialog to confirm the restore.",
  "backups.restoreDialog.blockedNoStop":
    "Ask an operator to stop the server, then reopen this dialog to confirm the restore.",
  "backups.restoreDialog.stop": "Stop server",
  "backups.restoreDialog.stopping": "Stopping the server…",
  "backups.restoreDialog.body":
    "This overwrites the server's current data with this backup. This cannot be undone.",
  "backups.restoreDialog.prompt": "Type RESTORE to confirm",
  "backups.restoreDialog.phrase": "RESTORE",
  "backups.restoreDialog.confirm": "Restore backup",
  // Delete dialog (typed confirm).
  "backups.deleteDialog.title": "Delete backup",
  "backups.deleteDialog.body":
    "This permanently deletes the backup archive. This cannot be undone.",
  "backups.deleteDialog.prompt": "Type DELETE to confirm",
  "backups.deleteDialog.phrase": "DELETE",
  "backups.deleteDialog.confirm": "Delete backup",
  // Outcomes (toasts).
  "backups.created": "Backup created.",
  "backups.uploaded": "Backup uploaded.",
  "backups.deleted": "Backup deleted.",
  "backups.restored": "Backup restored.",
  "backups.error.notStopped": "Stop the server before restoring a backup.",
  "backups.error.unsettled":
    "The server is settling — try again once it is stopped or running.",
  "backups.error.invalidArchive": "That file is not a valid backup archive.",
  "backups.error.workerUnavailable":
    "No worker is available to take the backup right now.",
  "backups.error.invalidSchedule":
    "backup_interval_hours must be a whole number of hours of at least 1.",
  "backups.error.generic": "Something went wrong. Please try again.",

  // Files tab (WEBUI_SPEC.md 6.6). One contiguous block to keep merge
  // conflicts with sibling tab PRs minimal.
  "files.denied": "You do not have permission to view this server's files.",
  "files.runningNotice":
    "Server is running — file changes go to the live working set and may need a restart to take effect.",
  "files.root": "root",
  "files.loading": "Loading…",
  "files.listError": "Could not list this directory. Try refreshing.",
  "files.openError": "Could not open this file.",
  "files.empty": "This directory is empty.",
  "files.noSelection": "Select a file to view or edit.",
  "files.truncated": "Listing truncated — too many entries to show them all.",
  "files.binary": "Binary file — download to view.",
  "files.editorLabel": "File contents",
  "files.upload": "Upload",
  "files.extractZip": "Extract ZIP",
  "files.newFolder": "New folder",
  "files.folderName": "Folder name",
  "files.newName": "New name",
  "files.create": "Create",
  "files.rename": "Rename",
  "files.delete": "Delete",
  "files.download": "Download",
  "files.save": "Save",
  "files.delete.dialogTitle": "Delete file",
  "files.delete.dialogBody":
    "This permanently deletes the selected file or directory. Type its name to confirm.",
  "files.delete.confirm": "Delete permanently",
  "files.delete.prompt": "Type the name to enable deletion",
  // Search (files/search).
  "files.search.label": "Search files",
  "files.search.placeholder": "Search by name…",
  "files.search.byName": "Name",
  "files.search.byContent": "Content",
  "files.search.submit": "Search",
  "files.search.empty": "No files matched.",
  "files.search.truncated":
    "Showing the first results — narrow your search to see more.",
  "files.search.error": "Search failed. Try again.",
  // History drawer + rollback (files/history, files/rollback).
  "files.history": "History",
  "files.history.title": "Version history",
  "files.history.loading": "Loading versions…",
  "files.history.error": "Could not load version history.",
  "files.history.empty": "No prior versions retained yet.",
  "files.history.hint":
    "Only the most recent versions are kept (10 by default); older ones are discarded.",
  "files.history.rollback": "Roll back",
  "files.history.close": "Close",
  "files.rollback.dialogTitle": "Roll back file",
  "files.rollback.dialogBody":
    "This replaces the current file with the selected version. The server must be stopped.",
  "files.rollback.confirm": "Roll back now",
  // Outcomes (toasts).
  "files.saved": "File saved.",
  "files.uploaded": "Upload complete.",
  "files.folderCreated": "Folder created.",
  "files.renamed": "Renamed.",
  "files.deleted": "Deleted.",
  "files.rolledBack": "Rolled back to the selected version.",
  "files.error.generic": "Something went wrong. Please try again.",

  // Players tab — attached op/whitelist groups (issue #453, WEBUI_SPEC.md 6.8).
  // One contiguous block to keep merge conflicts with sibling i18n PRs minimal.
  "players.heading": "Attached groups",
  "players.loading": "Loading groups…",
  "players.loadError": "Could not load groups. Try refreshing.",
  "players.empty": "No groups are attached to this server yet.",
  "players.kind.op": "op",
  "players.kind.whitelist": "whitelist",
  // Member count shown next to each group (the group's player list length).
  "players.memberCount": "members",
  "players.detach": "Detach",
  "players.detached": "Group detached.",
  // Attach picker: community groups not yet attached to this server.
  "players.attachHeading": "Attach a group",
  "players.attachEmpty": "All of this community's groups are already attached.",
  "players.attach": "Attach",
  "players.attached": "Group attached.",
  // Inline pointer to the full Groups management surface (Phase 6).
  "players.manageHint": "Create and edit groups in community settings.",
  "players.manageLink": "Community settings",
  "players.error.generic": "Something went wrong. Please try again.",

  // Server create wizard (WEBUI_SPEC.md 6.3). One contiguous block to keep
  // merge conflicts with sibling i18n PRs minimal.
  "serverCreate.subtitle": "Provision a new Minecraft server.",
  "serverCreate.denied": "You do not have permission to create servers.",
  "serverCreate.tab.new": "New server",
  "serverCreate.tab.import": "Import ZIP",
  // Wizard step rail.
  "serverCreate.step.type": "Type & version",
  "serverCreate.step.runtime": "Runtime",
  "serverCreate.step.config": "Config & EULA",
  "serverCreate.next": "Next",
  "serverCreate.back": "Back",
  "serverCreate.create": "Create server",
  "serverCreate.creating": "Creating…",
  // Step 1 — type & version.
  "serverCreate.typeHeading": "Server type",
  "serverCreate.typeLoading": "Loading server types…",
  "serverCreate.typeLoadError":
    "Could not load the version catalog. Try refreshing.",
  "serverCreate.versionLabel": "Minecraft version",
  "serverCreate.versionLoading": "Loading versions…",
  "serverCreate.versionLoadError": "Could not load versions for this type.",
  "serverCreate.spigotHint":
    "No official distribution API — use Paper instead.",
  "serverCreate.type.vanilla": "Vanilla",
  "serverCreate.type.paper": "Paper",
  "serverCreate.type.fabric": "Fabric",
  "serverCreate.type.forge": "Forge",
  "serverCreate.type.spigot": "Spigot",
  "serverCreate.typeSub.vanilla": "official",
  "serverCreate.typeSub.paper": "performance fork",
  "serverCreate.typeSub.fabric": "light modding",
  "serverCreate.typeSub.forge": "heavy modding",
  "serverCreate.typeSub.spigot": "unsupported",
  // Step 2 — runtime.
  "serverCreate.backendLabel": "Execution backend",
  "serverCreate.backend.host_process": "Host process",
  "serverCreate.backend.container": "Container",
  "serverCreate.portLabel": "Game port",
  "serverCreate.portHint": "Auto-suggested from the next free port.",
  "serverCreate.portChecking": "Checking port availability…",
  "serverCreate.portAvailable": "Port is available.",
  "serverCreate.portTaken": "Port is already in use.",
  "serverCreate.portOutOfRange": "Port is outside the allowed range.",
  "serverCreate.portCheckFailed": "Could not check that port.",
  // Step 3 — config & EULA.
  "serverCreate.nameLabel": "Server name",
  "serverCreate.namePlaceholder": "survival",
  "serverCreate.propsHeading": "server.properties overrides",
  "serverCreate.propsHint":
    "Optional. Keys written into server.properties on first boot.",
  "serverCreate.propKeyPlaceholder": "key (e.g. motd)",
  "serverCreate.propValuePlaceholder": "value",
  "serverCreate.propAdd": "Add override",
  "serverCreate.propRemove": "Remove",
  "serverCreate.eulaLabel": "I accept the Minecraft EULA.",
  "serverCreate.eulaWarning":
    "Without accepting the EULA the server is created but cannot start until you accept it later.",
  // Create error surfacing.
  "serverCreate.error.spigot_unsupported":
    "Spigot is not supported — use Paper instead.",
  "serverCreate.error.port_taken":
    "That game port is already in use. Pick another.",
  "serverCreate.error.port_out_of_range":
    "That game port is outside the allowed range.",
  "serverCreate.error.server_name_exists":
    "A server with that name already exists in this community.",
  "serverCreate.error.invalid_server_name": "That server name is not allowed.",
  "serverCreate.error.unknown_version":
    "That version is not available for this type.",
  "serverCreate.genericError": "Could not create the server. Please try again.",
  // Import tab.
  "serverCreate.import.heading": "Import from a ZIP export",
  "serverCreate.import.hint":
    "Upload a ZIP exported from another instance. The name and backend below apply; the EULA is never carried over.",
  "serverCreate.import.fileLabel": "Export archive (.zip)",
  "serverCreate.import.submit": "Import server",
  "serverCreate.import.importing": "Importing…",
  "serverCreate.import.noFile": "Choose a ZIP file to import.",
  "serverCreate.import.error.invalid_export_metadata":
    "That archive is not a valid server export.",
  "serverCreate.import.tooLarge": "That archive is too large to import.",

  // Community settings (WEBUI_SPEC.md 6.10) — one contiguous block to minimise
  // merge conflicts with the sibling Roles/Grants/Groups/Audit tab PRs.
  "communitySettings.loading": "Loading…",
  "communitySettings.loadError":
    "Could not load this community. Try refreshing.",
  "communitySettings.breadcrumb": "Dashboard",
  "communitySettings.tab.members": "Members",
  "communitySettings.tab.roles": "Roles",
  "communitySettings.tab.grants": "Grants",
  "communitySettings.tab.groups": "Groups",
  "communitySettings.tab.audit": "Audit log",
  "communitySettings.tab.general": "General",

  // Members tab.
  "communitySettings.members.heading": "Members",
  "communitySettings.members.loading": "Loading members…",
  "communitySettings.members.loadError": "Could not load members.",
  "communitySettings.members.empty": "No members yet.",
  "communitySettings.members.colUsername": "Username",
  "communitySettings.members.colRoles": "Roles",
  "communitySettings.members.unknownUser": "(unknown user)",
  "communitySettings.members.add": "Add member…",
  "communitySettings.members.remove": "Remove",
  "communitySettings.members.unassignRole": "Remove role",
  "communitySettings.members.assignRole": "Assign role",
  "communitySettings.members.noRolesLeft": "All roles assigned.",
  "communitySettings.members.addDialogTitle": "Add a member",
  "communitySettings.members.addDialogBody":
    "Add an existing user to this community by their exact username.",
  "communitySettings.members.usernameLabel": "Username",
  "communitySettings.members.usernamePlaceholder": "username",
  "communitySettings.members.addSubmit": "Add member",
  "communitySettings.members.addEmpty": "Enter a username.",
  "communitySettings.members.errUserNotFound": "No user with that username.",
  "communitySettings.members.errAlreadyMember":
    "That user is already a member of this community.",
  "communitySettings.members.errGeneric":
    "Could not add the member. Please try again.",
  "communitySettings.members.added": "Member added.",
  "communitySettings.members.removeDialogTitle": "Remove member",
  "communitySettings.members.removeDialogBody":
    "Removing this member revokes all their roles and per-server grants in this community. This cannot be undone.",
  "communitySettings.members.removeConfirm": "Remove member",
  "communitySettings.members.removePrompt":
    "Type the username to enable removal",
  "communitySettings.members.removed": "Member removed.",
  "communitySettings.members.roleError":
    "Could not update roles. Please try again.",

  // Audit tab (WEBUI_SPEC.md 6.10).
  "communitySettings.audit.heading": "Audit log",
  "communitySettings.audit.loading": "Loading audit log…",
  "communitySettings.audit.loadError": "Could not load the audit log.",
  "communitySettings.audit.empty": "No matching audit entries.",
  "communitySettings.audit.filterOperation": "Operation",
  "communitySettings.audit.filterOperationPlaceholder": "e.g. server:start",
  "communitySettings.audit.filterActor": "Actor ID",
  "communitySettings.audit.filterActorPlaceholder": "user id",
  "communitySettings.audit.filterSince": "Since",
  "communitySettings.audit.filterUntil": "Until",
  "communitySettings.audit.apply": "Apply filters",
  "communitySettings.audit.colTime": "Time",
  "communitySettings.audit.colActor": "Actor",
  "communitySettings.audit.colOperation": "Operation",
  "communitySettings.audit.colOutcome": "Outcome",
  "communitySettings.audit.colTarget": "Target",
  "communitySettings.audit.systemActor": "(system)",
  "communitySettings.audit.prev": "Previous",
  "communitySettings.audit.next": "Next",

  // General tab.
  "communitySettings.general.heading": "General",
  "communitySettings.general.nameLabel": "Community name",
  "communitySettings.general.save": "Save name",
  "communitySettings.general.saved": "Community renamed.",
  "communitySettings.general.nameTaken": "That name is already taken.",
  "communitySettings.general.invalidName": "That name is not allowed.",
  "communitySettings.general.saveError":
    "Could not rename the community. Please try again.",
  "communitySettings.general.dangerHeading": "Danger zone",
  "communitySettings.general.deleteTitle": "Delete community",
  "communitySettings.general.deleteDesc":
    "Deletes all servers, backups, roles and memberships of this community.",
  "communitySettings.general.deleteButton": "Delete…",
  "communitySettings.general.deleteDialogTitle": "Delete community",
  "communitySettings.general.deleteDialogBody":
    "This permanently deletes the community and everything in it. This cannot be undone.",
  "communitySettings.general.deleteConfirm": "Delete community",
  "communitySettings.general.deletePrompt":
    "Type the community name to enable deletion",
  "communitySettings.general.deleted": "Community deleted.",
  "communitySettings.general.deleteError":
    "Could not delete the community. Please try again.",

  // Permission / authorization feedback (WEBUI_SPEC.md 7.3 / 7.4)
  "permissions.denied": "You do not have permission to do that.",
  // Composed with the missing permission code, e.g. "You lack: server:start".
  "permissions.deniedNamed": "You lack: ",
} as const;
