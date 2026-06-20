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
  "shell.language": "Language",
  "shell.language.en": "English",
  "shell.language.ja": "日本語",

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
  "nav.sharedResources": "Shared resources",

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

  // Not-found (404) route: shown for any unmatched URL (#639). The body
  // explains the state and the link returns the user to the landing route.
  "notFound.body": "The page you are looking for does not exist or has moved.",
  "notFound.home": "Back to home",

  // No-community empty state (#584): shown on the landing route when the
  // signed-in account belongs to zero communities.
  "noCommunity.title": "No community yet",
  "noCommunity.body":
    "Your account isn't a member of any community yet. Communities scope your servers, members, and settings.",
  "noCommunity.memberHint":
    "Ask a platform administrator to add you to a community.",
  "noCommunity.adminHint":
    "As a platform administrator, you can create the first community.",
  "noCommunity.adminCta": "Create a community",

  // Community-not-found state (#784): shown when a URL `:cid` is not one the
  // signed-in account belongs to (stale bookmark, or a community the user has
  // left). The dashboard and server-create pages derive their community from the
  // URL, so they render this instead of silently falling back to another one.
  "community.notFound.title": "Community not found",
  "community.notFound.body":
    "This community does not exist or you are not a member of it.",

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
  "login.submit": "Sign in",
  "login.submitting": "Signing in…",
  "login.invalidCredentials": "Invalid username or password.",
  "login.genericError": "Could not sign in. Please try again.",
  "login.sessionExpired": "Your session expired. Please sign in again.",
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
  "common.showPassword": "Show password",
  "common.hidePassword": "Hide password",
  "common.resizeColumn": "Drag to resize, double-click to reset",
  "common.chooseFile": "Choose file",
  "common.noFileChosen": "No file chosen",

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
    "This permanently deletes your account. This cannot be undone. Type your username and enter your password to confirm.",
  "account.delete.confirm": "Delete account",
  "account.delete.prompt": "Type your username to enable deletion",
  "account.delete.password": "Confirm your password",

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
  // Card/table view toggle (#541); cards remain the default.
  "dashboard.view.label": "Server list view",
  "dashboard.view.cards": "Cards",
  "dashboard.view.table": "Table",
  // Table-view column headers (#541): the same data as the cards.
  "dashboard.col.name": "Name",
  "dashboard.col.state": "State",
  "dashboard.col.type": "Type / version",
  "dashboard.col.backend": "Backend",
  "dashboard.col.port": "Port",
  "dashboard.col.address": "Address",
  "dashboard.col.worker": "Worker",
  "dashboard.col.actions": "Actions",
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
  // 503 service-unavailable reasons (issue #1092): post-restart scenarios where
  // the Worker or JAR backend is not yet ready.
  "dashboard.lifecycle.noEligibleWorker":
    "No worker is available. If the system just restarted, wait a moment and try again.",
  "dashboard.lifecycle.workerUnavailable":
    "Could not reach the worker. Please wait a moment and try again.",
  "dashboard.lifecycle.jarUnavailable":
    "Could not provision the server JAR. Please wait a moment and try again.",
  // Live-status degraded indicator: WS down, polling fallback (SPEC 6.2 / 7.2).
  "dashboard.liveDegraded": "Live updates degraded — polling",
  // Clickable join-hostname copy feedback.
  "dashboard.copiedJoinHostname": "Copied!",
  // Filter and sort controls (#1123).
  "dashboard.filter.search": "Search by name…",
  "dashboard.filter.state": "Filter by state",
  "dashboard.filter.noMatch": "No servers match the current filters.",
  "dashboard.sort.label": "Sort",
  "dashboard.sort.name": "Name",
  "dashboard.sort.state": "State",
  "dashboard.sort.type": "Type",

  // Server detail page (WEBUI_SPEC.md 6.4 / 6.9). One contiguous block to keep
  // merge conflicts with sibling i18n PRs minimal.
  "serverDetail.loading": "Loading server…",
  "serverDetail.loadError": "Could not load this server. Try refreshing.",
  "serverDetail.breadcrumb": "Servers",
  // Overview header.
  "serverDetail.crashDetail": "Crash reason:",
  "serverDetail.converging": "settling…",
  "serverDetail.desired": "desired",
  "serverDetail.observed": "observed",
  "serverDetail.noWorker": "no worker assigned",
  "serverDetail.worker": "Worker",
  "serverDetail.noPort": "no port",
  // Relay join hostname (issue #961): shown in the header when the relay is enabled.
  "serverDetail.copiedJoinHostname": "Copied!",
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
  // Before the first metrics frame arrives, vs. no stream while not running.
  "serverDetail.metric.collecting": "Collecting…",
  "serverDetail.metric.idle": "No metrics while stopped",
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
  "serverDetail.forceStop.dialogTitle": "Force stop server?",
  "serverDetail.forceStop.dialogBody":
    "Force stop kills the server process immediately without a graceful shutdown. Unsaved data (chunks, player progress) may be lost.",
  "serverDetail.forceStop.confirm": "Force Stop",
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
  // Per-server memory limit (issue #709). Unset means the JVM picks its own
  // default heap, so the field reads as "driver default" rather than 0/blank.
  "serverDetail.settings.memoryLimit": "Memory limit (MiB)",
  "serverDetail.settings.memoryLimitDefault": "Driver default",
  "serverDetail.settings.memoryLimitHint":
    "Maximum memory for this server, in MiB. Leave blank to use the driver default.",
  "serverDetail.settings.memoryLimitRange":
    "Enter a whole number between 512 and 1048576 MiB, or leave blank for the driver default.",
  // Per-server CPU allocation (issue #726). A soft, relative share of host CPU
  // in millicores (1000 = one core), not a hard cap; unset reads as "auto".
  "serverDetail.settings.cpuAllocation": "CPU allocation (millicores)",
  "serverDetail.settings.cpuAllocationDefault": "Auto",
  "serverDetail.settings.cpuAllocationHint":
    "Soft share of CPU for this server, in millicores (1000 = one core). A relative weight under load, not a hard cap — the server can use more when the host is idle. Leave blank for auto.",
  "serverDetail.settings.cpuAllocationRange":
    "Enter a whole number between 100 and 128000 millicores, or leave blank for auto.",
  // Relay join-address name field (issue #961): visible only when relay is enabled.
  "serverDetail.settings.slug": "Join address name",
  "serverDetail.settings.slugHint":
    "Lowercase letters, numbers and hyphens only. Cannot start or end with a hyphen. Leave unchanged to keep the current join address.",
  "serverDetail.settings.slugInvalid":
    "Must be a valid DNS label: lowercase letters, digits, hyphens; cannot start or end with a hyphen.",
  "serverDetail.settings.slugTaken":
    "This join address name is already in use.",
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
  "serverDetail.error.invalidMemoryLimit":
    "The memory limit must be a whole number between 512 and 1048576 MiB.",
  "serverDetail.error.invalidCpuAllocation":
    "The CPU allocation must be a whole number between 100 and 128000 millicores.",
  // Relay join address name errors (issue #961).
  "serverDetail.error.invalidSlug":
    "The join address name must be a valid DNS label: lowercase letters, digits, hyphens; cannot start or end with a hyphen.",
  "serverDetail.error.slugTaken": "That join address name is already in use.",
  "serverDetail.error.generic": "Something went wrong. Please try again.",
  // EULA acceptance dialog (issue #1104).
  "serverDetail.eulaDialog.title": "Accept Minecraft EULA?",
  "serverDetail.eulaDialog.body":
    "You must accept the Minecraft End User License Agreement (EULA) before starting this server.",
  "serverDetail.eulaDialog.accept": "Accept and start",
  "serverDetail.eulaDialog.link": "View the Minecraft EULA",

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
  // Shown beside the total when some backups have no recorded size (legacy
  // NULL-size rows, #281): the figure sums only the known sizes (#640).
  "backups.stat.totalSizePartial": "known only",
  "backups.stat.newest": "Newest",
  "backups.stat.oldest": "Oldest",
  // Table.
  "backups.col.created": "Created",
  "backups.col.source": "Source",
  "backups.col.condition": "Condition",
  "backups.col.size": "Size",
  "backups.col.creator": "By",
  "backups.unknownSize": "unknown",
  "backups.unknownCreator": "—",
  // Condition badge (API `health`: healthy / quarantined / unknown). Plain
  // language — no internal jargon. A healthy backup shows nothing, keeping the
  // row quiet; only the at-risk states are flagged.
  "backups.health.quarantined": "Damaged",
  "backups.health.quarantinedTitle":
    "This backup's data is known to be damaged. Restoring it may produce a broken world.",
  "backups.health.unknown": "Unverified",
  "backups.health.unknownTitle":
    "This backup has not been checked, so its condition is unknown.",
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
  // Force-restore warning shown only when the chosen backup is quarantined
  // (health === "quarantined"). It restores anyway with force=true, so the copy
  // makes the deliberate, damaged-data nature explicit (#745).
  "backups.restoreDialog.damagedWarning":
    "This backup's data is known to be damaged. Restoring it may leave the server with a broken world, and there is no way to repair it afterwards.",
  "backups.restoreDialog.damagedConfirm": "Restore the damaged backup anyway",
  // Acknowledgement checkbox label gating the force-restore — affirmation phrased
  // (the user asserts they accept the risk), not a restatement of the warning.
  "backups.restoreDialog.damagedAck":
    "I understand this backup is damaged and may leave a broken world that cannot be repaired.",
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
  "backups.error.tooLarge": "That file exceeds the 512 MiB upload limit.",
  "backups.error.generic": "Something went wrong. Please try again.",

  // Files tab (WEBUI_SPEC.md 6.6). One contiguous block to keep merge
  // conflicts with sibling tab PRs minimal.
  "files.denied": "You do not have permission to view this server's files.",
  "files.runningNotice":
    "Server is running — file edits go to the live working set. Upload and folder creation require stopping the server first.",
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
  "files.error.serverMustBeStopped":
    "Stop the server before uploading files or creating folders.",
  "files.error.tooLarge": "That file exceeds the 512 MiB upload limit.",
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
  // Distinct from attachEmpty: the community has no groups at all (issue #642).
  "players.attachNoGroups": "This community has no groups yet.",
  "players.attach": "Attach",
  "players.attached": "Group attached.",
  // Inline pointer to the full Groups management surface (Phase 6).
  "players.manageHint": "Create and edit groups in community settings.",
  "players.manageLink": "Community settings",
  "players.error.generic": "Something went wrong. Please try again.",

  // Sessions view — relay game session history (issue #961).
  // Rendered only for members holding session:read; identity columns are
  // the claimed Login Start values (unverified).
  "sessions.heading": "Sessions",
  "sessions.loading": "Loading sessions…",
  "sessions.loadError": "Could not load sessions. Try refreshing.",
  "sessions.empty": "No sessions recorded yet.",
  "sessions.col.hostname": "Hostname",
  "sessions.col.playerIp": "IP (claimed)",
  "sessions.col.username": "Username (claimed)",
  "sessions.col.start": "Start",
  "sessions.col.end": "End",
  "sessions.valueUnknown": "—",
  "sessions.active": "active",
  // Pagination controls.
  "sessions.prev": "Previous",
  "sessions.next": "Next",

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
  // Per-server resource allocation in the create wizard (issue #715), mirroring
  // the Settings tab: a memory limit (MiB) and a soft CPU share (millicores).
  // Both are optional — blank means the driver default / auto.
  "serverCreate.memoryLimitLabel": "Memory limit (MiB)",
  "serverCreate.memoryLimitDefault": "Driver default",
  "serverCreate.memoryLimitHint":
    "Maximum memory for this server, in MiB. Leave blank to use the driver default.",
  "serverCreate.memoryLimitRange":
    "Enter a whole number between 512 and 1048576 MiB, or leave blank for the driver default.",
  "serverCreate.cpuAllocationLabel": "CPU allocation (millicores)",
  "serverCreate.cpuAllocationDefault": "Auto",
  "serverCreate.cpuAllocationHint":
    "Soft share of CPU for this server, in millicores (1000 = one core). A relative weight under load, not a hard cap — the server can use more when the host is idle. Leave blank for auto.",
  "serverCreate.cpuAllocationRange":
    "Enter a whole number between 100 and 128000 millicores, or leave blank for auto.",
  // Optional join address name (slug) at create time (issue #981).
  "serverCreate.slugLabel": "Join address name (optional)",
  "serverCreate.slugPlaceholder": "e.g. myserver",
  "serverCreate.slugHint":
    "Lowercase letters, numbers and hyphens only. Leave blank to generate a random address.",
  "serverCreate.slugInvalid":
    "Must be a valid DNS label: lowercase letters, digits, hyphens; cannot start or end with a hyphen.",
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
  "serverCreate.error.invalid_memory_limit":
    "The memory limit must be a whole number between 512 and 1048576 MiB.",
  "serverCreate.error.invalid_cpu_allocation":
    "The CPU allocation must be a whole number between 100 and 128000 millicores.",
  "serverCreate.error.invalid_slug":
    "That join address name is not valid. Use lowercase letters, digits and hyphens only.",
  "serverCreate.error.slug_taken":
    "That join address name is already in use. Choose another.",
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
  "communitySettings.members.removeError":
    "Could not remove the member. Please try again.",
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
  "communitySettings.audit.filterActorInvalid": "must be a user id (UUID)",
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
  // Human-readable labels for the audit `operation` codes (the `<resource>:<action>`
  // constants in api audit/domain/operations.py) and the `target_type` prefixes.
  // An unmapped code falls back to its raw value (auditShared.tsx), so the table
  // never breaks if the backend adds a new code ahead of this dictionary (#643).
  "communitySettings.audit.op.auth:login": "Sign in",
  "communitySettings.audit.op.auth:logout": "Sign out",
  "communitySettings.audit.op.auth:register": "Register account",
  "communitySettings.audit.op.auth:refresh": "Refresh session",
  "communitySettings.audit.op.auth:refresh_reuse": "Refresh token reuse",
  "communitySettings.audit.op.auth:session_restore": "Restore session",
  "communitySettings.audit.op.auth:password_change": "Change password",
  "communitySettings.audit.op.auth:profile_update": "Update profile",
  "communitySettings.audit.op.auth:account_delete": "Delete account",
  "communitySettings.audit.op.auth:session_revoke": "Revoke session",
  "communitySettings.audit.op.user:create": "Create user",
  "communitySettings.audit.op.user:deactivate": "Deactivate user",
  "communitySettings.audit.op.user:reactivate": "Reactivate user",
  "communitySettings.audit.op.user:delete": "Delete user",
  "communitySettings.audit.op.user:platform_admin_grant":
    "Grant platform admin",
  "communitySettings.audit.op.user:platform_admin_revoke":
    "Revoke platform admin",
  "communitySettings.audit.op.community:provision": "Create community",
  "communitySettings.audit.op.community:update": "Update community",
  "communitySettings.audit.op.community:delete": "Delete community",
  "communitySettings.audit.op.member:add": "Add member",
  "communitySettings.audit.op.member:remove": "Remove member",
  "communitySettings.audit.op.role:assign": "Assign role",
  "communitySettings.audit.op.role:unassign": "Unassign role",
  "communitySettings.audit.op.role:create": "Create role",
  "communitySettings.audit.op.role:update": "Update role",
  "communitySettings.audit.op.role:delete": "Delete role",
  "communitySettings.audit.op.grant:create": "Create grant",
  "communitySettings.audit.op.grant:revoke": "Revoke grant",
  "communitySettings.audit.op.server:create": "Create server",
  "communitySettings.audit.op.server:update": "Update server",
  "communitySettings.audit.op.server:delete": "Delete server",
  "communitySettings.audit.op.server:start": "Start server",
  "communitySettings.audit.op.server:stop": "Stop server",
  "communitySettings.audit.op.server:restart": "Restart server",
  "communitySettings.audit.op.server:command": "Send console command",
  "communitySettings.audit.op.server:export": "Export server",
  "communitySettings.audit.op.server:import": "Import server",
  "communitySettings.audit.op.backup:create": "Create backup",
  "communitySettings.audit.op.backup:restore": "Restore backup",
  "communitySettings.audit.op.backup:delete": "Delete backup",
  "communitySettings.audit.op.backup:upload": "Upload backup",
  "communitySettings.audit.op.backup:download": "Download backup",
  "communitySettings.audit.op.file:write": "Edit file",
  "communitySettings.audit.op.file:rollback": "Roll back file",
  "communitySettings.audit.op.file:upload": "Upload file",
  "communitySettings.audit.op.file:download": "Download file",
  "communitySettings.audit.op.file:rename": "Rename file",
  "communitySettings.audit.op.file:delete": "Delete file",
  "communitySettings.audit.op.file:mkdir": "Create folder",
  "communitySettings.audit.op.file:search": "Search files",
  "communitySettings.audit.op.version:refresh": "Refresh version catalog",
  "communitySettings.audit.op.version:jar_gc": "Clean up JAR pool",
  "communitySettings.audit.op.worker:drain_set": "Drain worker",
  "communitySettings.audit.op.worker:drain_clear": "Undrain worker",
  "communitySettings.audit.op.group:create": "Create player group",
  "communitySettings.audit.op.group:update": "Update player group",
  "communitySettings.audit.op.group:delete": "Delete player group",
  "communitySettings.audit.op.group:player_add": "Add player to group",
  "communitySettings.audit.op.group:player_remove": "Remove player from group",
  "communitySettings.audit.op.group:attach": "Attach player group",
  "communitySettings.audit.op.group:detach": "Detach player group",
  "communitySettings.audit.targetType.community": "Community",
  "communitySettings.audit.targetType.user": "User",
  "communitySettings.audit.targetType.role": "Role",
  "communitySettings.audit.targetType.grant": "Grant",
  "communitySettings.audit.targetType.server": "Server",
  "communitySettings.audit.targetType.backup": "Backup",
  "communitySettings.audit.targetType.worker": "Worker",
  "communitySettings.audit.targetType.file": "File",
  "communitySettings.audit.targetType.group": "Player group",

  // Roles tab.
  "communitySettings.roles.heading": "Roles",
  "communitySettings.roles.loading": "Loading roles…",
  "communitySettings.roles.loadError": "Could not load roles.",
  "communitySettings.roles.empty": "No roles yet.",
  "communitySettings.roles.create": "New role…",
  "communitySettings.roles.preset": "Preset",
  "communitySettings.roles.edit": "Edit",
  "communitySettings.roles.delete": "Delete",
  "communitySettings.roles.createDialogTitle": "New role",
  "communitySettings.roles.editDialogTitle": "Edit role",
  "communitySettings.roles.nameLabel": "Role name",
  "communitySettings.roles.namePlaceholder": "e.g. Moderator",
  "communitySettings.roles.permissionsLabel": "Permissions",
  "communitySettings.roles.selectAll": "Select all",
  "communitySettings.roles.save": "Save role",
  "communitySettings.roles.created": "Role created.",
  "communitySettings.roles.updated": "Role updated.",
  "communitySettings.roles.deleted": "Role deleted.",
  "communitySettings.roles.nameEmpty": "Enter a role name.",
  "communitySettings.roles.errNameTaken": "That name is already taken.",
  "communitySettings.roles.errInvalidName": "That name is not allowed.",
  "communitySettings.roles.errPreset": "Preset roles cannot be changed.",
  "communitySettings.roles.errGeneric":
    "Could not save the role. Please try again.",
  "communitySettings.roles.deleteError":
    "Could not delete the role. Please try again.",
  "communitySettings.roles.deleteDialogTitle": "Delete role",
  "communitySettings.roles.deleteDialogBody":
    "Deleting this role removes it from every member who holds it. This cannot be undone.",
  "communitySettings.roles.deleteConfirm": "Delete role",
  "communitySettings.roles.deletePrompt":
    "Type the role name to enable deletion",
  // Permission family group labels (WEBUI_SPEC.md 2.2).
  "communitySettings.roles.family.server": "Servers",
  "communitySettings.roles.family.file": "Files",
  "communitySettings.roles.family.backup": "Backups",
  "communitySettings.roles.family.member": "Members",
  "communitySettings.roles.family.role": "Roles",
  "communitySettings.roles.family.grant": "Grants",
  "communitySettings.roles.family.group": "Groups",
  "communitySettings.roles.family.community": "Community",
  "communitySettings.roles.family.audit": "Audit log",
  // Session family (issue #961): relay game session history.
  "communitySettings.roles.family.session": "Sessions",
  // Permission code labels (the action within each family).
  "communitySettings.roles.code.server:create": "Create",
  "communitySettings.roles.code.server:read": "Read",
  "communitySettings.roles.code.server:update": "Update",
  "communitySettings.roles.code.server:delete": "Delete",
  "communitySettings.roles.code.server:start": "Start",
  "communitySettings.roles.code.server:stop": "Stop",
  "communitySettings.roles.code.server:restart": "Restart",
  "communitySettings.roles.code.server:command": "Send command",
  "communitySettings.roles.code.file:read": "Read",
  "communitySettings.roles.code.file:edit": "Edit",
  "communitySettings.roles.code.file:history": "View history",
  "communitySettings.roles.code.file:rollback": "Roll back",
  "communitySettings.roles.code.backup:create": "Create",
  "communitySettings.roles.code.backup:read": "Read",
  "communitySettings.roles.code.backup:restore": "Restore",
  "communitySettings.roles.code.backup:delete": "Delete",
  "communitySettings.roles.code.backup:schedule": "Schedule",
  "communitySettings.roles.code.member:read": "Read",
  "communitySettings.roles.code.member:add": "Add",
  "communitySettings.roles.code.member:remove": "Remove",
  "communitySettings.roles.code.role:read": "Read",
  "communitySettings.roles.code.role:manage": "Manage",
  "communitySettings.roles.code.grant:read": "Read",
  "communitySettings.roles.code.grant:manage": "Manage",
  "communitySettings.roles.code.group:read": "Read",
  "communitySettings.roles.code.group:manage": "Manage",
  "communitySettings.roles.code.community:read": "Read",
  "communitySettings.roles.code.community:update": "Update",
  "communitySettings.roles.code.community:delete": "Delete",
  "communitySettings.roles.code.audit:read": "Read",
  // Session permission code label (issue #961).
  "communitySettings.roles.code.session:read": "Read",

  // Grants tab (WEBUI_SPEC.md 6.10): per-server permission grants.
  "communitySettings.grants.heading": "Grants",
  "communitySettings.grants.loading": "Loading grants…",
  "communitySettings.grants.loadError": "Could not load grants.",
  "communitySettings.grants.empty": "No grants yet.",
  "communitySettings.grants.create": "Grant access…",
  "communitySettings.grants.colMember": "Member",
  "communitySettings.grants.colServer": "Server",
  "communitySettings.grants.colPermissions": "Permissions",
  "communitySettings.grants.filterLabel": "Filter by member",
  "communitySettings.grants.filterAll": "All members",
  "communitySettings.grants.unknownUser": "(unknown user)",
  "communitySettings.grants.revoke": "Revoke",
  "communitySettings.grants.revoked": "Grant revoked.",
  "communitySettings.grants.revokeError":
    "Could not revoke the grant. Please try again.",
  "communitySettings.grants.revokeDialogTitle": "Revoke grant",
  "communitySettings.grants.revokeDialogBody":
    "This removes the member's per-server permissions on this server. This cannot be undone.",
  "communitySettings.grants.revokeConfirm": "Revoke grant",
  "communitySettings.grants.revokePrompt": "Type REVOKE to confirm",
  "communitySettings.grants.revokeConfirmPhrase": "REVOKE",
  "communitySettings.grants.createDialogTitle": "Grant per-server access",
  "communitySettings.grants.createDialogBody":
    "Grant a member extra permissions on one server, beyond their roles.",
  "communitySettings.grants.memberLabel": "Member",
  "communitySettings.grants.memberPlaceholder": "Select a member",
  "communitySettings.grants.serverLabel": "Server",
  "communitySettings.grants.serverPlaceholder": "Select a server",
  "communitySettings.grants.permissionsLabel": "Permissions",
  "communitySettings.grants.createSubmit": "Create grant",
  "communitySettings.grants.created": "Grant created.",
  "communitySettings.grants.createIncomplete":
    "Pick a member, a server, and at least one permission.",
  "communitySettings.grants.createError":
    "Could not create the grant. Please try again.",

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

  // Community settings — Groups tab (WEBUI_SPEC.md 6.10, issue #464)
  "communitySettings.groups.heading": "Player groups",
  "communitySettings.groups.loading": "Loading groups…",
  "communitySettings.groups.loadError": "Could not load groups.",
  "communitySettings.groups.empty": "No groups yet.",
  "communitySettings.groups.create": "New group…",
  "communitySettings.groups.kind.op": "op",
  "communitySettings.groups.kind.whitelist": "whitelist",
  "communitySettings.groups.memberCount": "players",
  "communitySettings.groups.expand": "Manage",
  "communitySettings.groups.collapse": "Close",
  "communitySettings.groups.rename": "Rename…",
  "communitySettings.groups.delete": "Delete",
  "communitySettings.groups.error": "Something went wrong. Please try again.",
  "communitySettings.groups.createDialogTitle": "New group",
  "communitySettings.groups.nameLabel": "Group name",
  "communitySettings.groups.namePlaceholder": "group name",
  "communitySettings.groups.kindLabel": "Kind",
  "communitySettings.groups.createSubmit": "Create group",
  "communitySettings.groups.nameEmpty": "Enter a group name.",
  "communitySettings.groups.created": "Group created.",
  "communitySettings.groups.renameDialogTitle": "Rename group",
  "communitySettings.groups.renameSubmit": "Save name",
  "communitySettings.groups.renamed": "Group renamed.",
  "communitySettings.groups.deleteDialogTitle": "Delete group",
  "communitySettings.groups.deleteDialogBody":
    "Deleting this group removes it from every server it is attached to. This cannot be undone.",
  "communitySettings.groups.deleteConfirm": "Delete group",
  "communitySettings.groups.deletePrompt": "Type the group name to confirm.",
  "communitySettings.groups.deleted": "Group deleted.",
  "communitySettings.groups.playersHeading": "Players",
  "communitySettings.groups.playersEmpty": "No players in this group yet.",
  "communitySettings.groups.removePlayer": "Remove",
  "communitySettings.groups.removePlayerDialogTitle": "Remove player",
  "communitySettings.groups.removePlayerDialogBody":
    "Are you sure you want to remove this player from the group?",
  "communitySettings.groups.removePlayerConfirm": "Remove player",
  "communitySettings.groups.playerRemoved": "Player removed.",
  "communitySettings.groups.addPlayer": "Add player",
  "communitySettings.groups.uuidLabel": "UUID",
  "communitySettings.groups.uuidPlaceholder": "player UUID",
  "communitySettings.groups.usernameLabel": "Username",
  "communitySettings.groups.usernamePlaceholder": "username",
  "communitySettings.groups.playerFieldsEmpty": "Enter a UUID and username.",
  "communitySettings.groups.playerAdded": "Player added.",
  "communitySettings.groups.serversHeading": "Attached servers",
  "communitySettings.groups.serversLoading": "Loading servers…",
  "communitySettings.groups.serversLoadError": "Could not load servers.",
  "communitySettings.groups.serversEmpty":
    "This group is not attached to any server yet.",
  "communitySettings.groups.detach": "Detach",
  "communitySettings.groups.detached": "Server detached.",
  "communitySettings.groups.attachHeading": "Attach a server",
  "communitySettings.groups.attachEmpty":
    "Every community server is already attached.",
  "communitySettings.groups.attach": "Attach",
  "communitySettings.groups.attached": "Server attached.",
  "communitySettings.groups.unknownServer": "(unknown server)",

  // Platform admin area (WEBUI_SPEC.md 6.12, Section 3) — #474
  "admin.denied.title": "Platform administrators only",
  "admin.denied.body": "You do not have access to the platform admin area.",
  "admin.overview.subtitle":
    "Fleet and global statistics — platform administrators only",
  "admin.overview.loading": "Loading platform statistics…",
  "admin.overview.loadError": "Could not load platform statistics.",
  "admin.overview.workers": "Workers",
  "admin.overview.workersOnline": "online",
  "admin.overview.workersDraining": "draining",
  "admin.overview.workersOffline": "offline",
  "admin.overview.servers": "Servers running",
  "admin.overview.serversHint": "assigned across the fleet",
  "admin.overview.backups": "Backups (global)",
  "admin.overview.jarPool": "jar pool",
  "admin.overview.jars": "jars",
  "admin.overview.fleet": "Worker fleet",
  "admin.overview.fleetWorker": "Worker",
  "admin.overview.fleetStatus": "Status",
  "admin.overview.fleetLoad": "Load",
  "admin.overview.fleetHeartbeat": "Heartbeat",
  "admin.overview.fleetEmpty": "No workers registered.",
  "admin.versions.subtitle":
    "Version catalog and the shared JAR cache — platform administrators only",
  "admin.versions.loading": "Loading version catalog…",
  "admin.versions.loadError": "Could not load the version catalog.",
  "admin.versions.catalog": "Server type catalog",
  "admin.versions.refreshAll": "Refresh all catalogs",
  "admin.versions.refresh": "Refresh",
  "admin.versions.refreshing": "Refreshing…",
  "admin.versions.type": "Server type",
  "admin.versions.count": "Versions",
  "admin.versions.latest": "Latest",
  "admin.versions.empty": "No server types catalogued.",
  "admin.versions.typeError": "unavailable",
  "admin.versions.refreshedAll":
    "Catalogs invalidated; listings refetch on next read.",
  // Composed with the server type, e.g. "Refreshed catalog: paper".
  "admin.versions.refreshedOne": "Refreshed catalog: ",
  "admin.versions.refreshError": "Could not refresh the catalog.",
  "admin.versions.jarPool": "JAR pool",
  "admin.versions.jarPoolCached": "Cached JARs",
  "admin.versions.jarPoolSize": "Total size",
  "admin.versions.gc": "Run garbage collection",
  "admin.versions.gcRunning": "Running…",
  "admin.versions.gcHint": "Removes JARs no longer referenced by any server.",
  "admin.versions.gcDialog.title": "Run JAR-pool garbage collection?",
  "admin.versions.gcDialog.body":
    "This deletes pooled JARs that no live server references. Unreferenced JARs are re-downloaded on demand.",
  "admin.versions.gcDialog.confirm": "Run GC",
  "admin.versions.gcDialog.promptLabel": "Type GC to confirm",
  // Composed with freed bytes + deleted count, e.g. "Reclaimed 412 MiB across 3 JARs.".
  "admin.versions.gcDoneReclaimed": "Reclaimed ",
  "admin.versions.gcDoneAcross": " across ",
  "admin.versions.gcDoneJars": " JARs.",
  "admin.versions.gcError": "Garbage collection failed.",
  // Communities (WEBUI_SPEC.md 6.12) — #476, #489
  "admin.communities.subtitle":
    "All communities on the platform. Provisioning is admin-only; self-service creation is not supported.",
  "admin.communities.loading": "Loading communities…",
  "admin.communities.loadError": "Could not load communities.",
  "admin.communities.empty": "No communities yet.",
  "admin.communities.colName": "Name",
  "admin.communities.colId": "ID",
  "admin.communities.colMembers": "Members",
  "admin.communities.colServers": "Servers",
  "admin.communities.colActions": "Actions",
  "admin.communities.delete": "Delete",
  "admin.communities.deleteTitle": "Delete community",
  "admin.communities.deleteBody":
    "This permanently deletes the community and everything in it (members, roles, servers). This cannot be undone.",
  "admin.communities.deletePrompt": "Type the community name to confirm:",
  "admin.communities.deleteConfirm": "Delete community",
  "admin.communities.deleted": "Community deleted.",
  "admin.communities.deleteError": "Could not delete the community.",
  "admin.communities.prev": "Previous",
  "admin.communities.next": "Next",
  "admin.communities.range": "{from}–{to} of {total}",
  "admin.communities.provision": "Provision community",
  "admin.communities.provisionSubmit": "Provision",
  "admin.communities.dialogTitle": "Provision community",
  "admin.communities.nameLabel": "Community name",
  "admin.communities.namePlaceholder": "e.g. Winter Server 2026",
  "admin.communities.ownerLabel": "Initial owner",
  "admin.communities.ownerPlaceholder": "Select an existing account…",
  "admin.communities.ownerHint":
    "The owner gets the preset Owner role (all community permissions).",
  "admin.communities.usersLoadError": "Could not load the user list.",
  // Truncation hint composed around the loaded/total counts, e.g.
  // "Showing the first 100 of 150 users." The owner picker requests the API
  // max page (100); when more accounts exist the later ones are omitted.
  "admin.communities.usersTruncatedPrefix": "Showing the first ",
  "admin.communities.usersTruncatedMid": " of ",
  "admin.communities.usersTruncatedSuffix": " users.",
  "admin.communities.provisioned": "Community provisioned.",
  "admin.communities.errNameRequired": "Enter a community name.",
  "admin.communities.errOwnerRequired": "Select an initial owner.",
  "admin.communities.errNameTaken":
    "A community with that name already exists.",
  "admin.communities.errInvalidName": "That community name is not valid.",
  "admin.communities.errOwnerNotFound": "That owner account no longer exists.",
  "admin.communities.errGeneric": "Could not provision the community.",

  // Workers fleet page (WEBUI_SPEC.md 6.12) — #477
  "admin.workers.subtitle":
    "Workers self-register over the control plane; drain to relocate servers before maintenance.",
  "admin.workers.loading": "Loading workers…",
  "admin.workers.loadError": "Could not load workers.",
  "admin.workers.empty": "No workers registered.",
  "admin.workers.colWorker": "Worker",
  "admin.workers.colStatus": "Status",
  "admin.workers.colVersion": "Version",
  "admin.workers.colDrivers": "Drivers",
  "admin.workers.colLoad": "Load",
  "admin.workers.colResources": "Resources",
  "admin.workers.colHeartbeat": "Heartbeat",
  "admin.workers.cpuCores": "c",
  "admin.workers.drain": "Drain",
  "admin.workers.undrain": "Undrain",
  "admin.workers.drainDialogTitle": "Drain worker",
  "admin.workers.drainDialogBody":
    "Draining stops new placements on this worker and stops its running servers with a final snapshot so they can be restarted elsewhere.",
  "admin.workers.drainConfirm": "Drain worker",
  "admin.workers.undrainDialogTitle": "Undrain worker",
  "admin.workers.undrainDialogBody":
    "Undraining lets this worker accept new placements again.",
  "admin.workers.undrainConfirm": "Undrain worker",
  "admin.workers.drained": "Worker draining.",
  // Appended after "Worker draining." when servers_stopped > 0, e.g.
  // "Worker draining. 3 servers marked — keep this worker connected until each is stopped and unassigned."
  "admin.workers.drainedCountSuffix":
    " servers marked — keep this worker connected until each is stopped and unassigned.",
  "admin.workers.drainDialogConvergenceWarning":
    "Stops and final snapshots run asynchronously (~120 s grace + a tick per server) and only while the worker stays connected. Keep this worker up until every formerly-assigned server reaches stopped and unassigned — shutting down early defers stops and snapshots to a reconnect that never happens in a decommission. Confirm convergence per server in the server list, not by the worker's load counter (which drops to 0 before any stop runs).",
  "admin.workers.undrained": "Worker undrained.",
  "admin.workers.drainError": "Could not drain the worker.",
  "admin.workers.undrainError": "Could not undrain the worker.",
  "admin.workers.notice":
    "Draining stops new placements on the worker; running servers are stopped with a final snapshot and can be restarted elsewhere. Offline workers reappear automatically when they reconnect.",

  // Admin Users page (WEBUI_SPEC.md 6.12) — user lifecycle, admin flag,
  // create-user dialog. One contiguous block (#475).
  "admin.users.subtitle": "Platform-wide user administration",
  "admin.users.loading": "Loading users…",
  "admin.users.loadError": "Could not load users.",
  "admin.users.empty": "No users.",
  "admin.users.count": "accounts",
  "admin.users.colUsername": "Username",
  "admin.users.colEmail": "Email",
  "admin.users.colStatus": "Status",
  "admin.users.colAdmin": "Admin",
  "admin.users.colCreated": "Created",
  "admin.users.you": "you",
  "admin.users.statusActive": "active",
  "admin.users.statusDeactivated": "deactivated",
  "admin.users.adminYes": "admin",
  "admin.users.adminNo": "—",
  "admin.users.prev": "‹ prev",
  "admin.users.next": "next ›",
  "admin.users.range": "{from}–{to} of {total}",
  "admin.users.deactivate": "Deactivate",
  "admin.users.reactivate": "Reactivate",
  "admin.users.makeAdmin": "Make admin",
  "admin.users.revokeAdmin": "Revoke admin",
  "admin.users.delete": "Delete",
  "admin.users.deactivated": "User deactivated.",
  "admin.users.reactivated": "User reactivated.",
  "admin.users.adminGranted": "Platform admin granted.",
  "admin.users.adminRevoked": "Platform admin revoked.",
  "admin.users.deleted": "User deleted.",
  // The API allows self-revoke of your own admin flag (only the last active
  // admin is protected); confirm before the operator locks themselves out.
  "admin.users.selfRevokeTitle": "Revoke your own admin?",
  "admin.users.selfRevokeBody":
    "You are about to revoke your own platform-admin access. You will lose access to the admin area immediately.",
  "admin.users.selfRevokeConfirm": "Revoke my admin",
  "admin.users.deleteTitle": "Delete user",
  "admin.users.deleteBody":
    "This permanently deletes the account. Type the username to confirm.",
  "admin.users.deletePrompt": "Username",
  "admin.users.deleteConfirm": "Delete user",
  // Conflict reasons the lifecycle routes return (admin_users.py).
  "admin.users.error.self_target":
    "You cannot do that to your own account here — use the account page.",
  "admin.users.error.last_platform_admin":
    "Cannot remove the last active platform admin.",
  "admin.users.error.owns_community":
    "This user owns a community and cannot be deleted.",
  "admin.users.error.not_found": "That user no longer exists.",
  "admin.users.error.generic": "The action could not be completed.",
  // Create-user dialog (POST /admin/users).
  "admin.users.create": "Create user",
  "admin.users.createTitle": "Create user",
  "admin.users.createSubmit": "Create",
  "admin.users.createSubmitting": "Creating…",
  "admin.users.created": "User created.",
  "admin.users.usernameLabel": "Username",
  "admin.users.emailLabel": "Email",
  "admin.users.passwordLabel": "Password",
  "admin.users.passwordHint":
    "At least 12 characters, mixing cases, digits, and symbols.",

  // Admin global Audit page (WEBUI_SPEC.md 6.12). The filter row, table and
  // paging strings are shared with the community Audit tab
  // (communitySettings.audit.*); only the community filter and column are new.
  "admin.audit.filterCommunity": "Community",
  "admin.audit.filterCommunityAll": "All communities",
  "admin.audit.colCommunity": "Community",
  // Truncation hint composed around the loaded/total counts, e.g. "Showing the
  // first 100 of 150 communities." The picker requests the API max page (100);
  // when more communities exist the later ones are omitted (#476/#488).
  "admin.audit.communitiesTruncatedPrefix": "Showing the first ",
  "admin.audit.communitiesTruncatedMid": " of ",
  "admin.audit.communitiesTruncatedSuffix": " communities.",

  // Plugins tab (issue #1153). One contiguous block to keep merge conflicts
  // with sibling i18n PRs minimal.
  "serverDetail.tab.plugins": "Plugins",
  "plugins.loading": "Loading plugins…",
  "plugins.loadError": "Could not load plugins.",
  "plugins.noRead": "You do not have permission to view plugins.",
  "plugins.empty": "No plugins installed.",
  "plugins.unsupported": "This server type does not support plugins or mods.",
  "plugins.serverNotStopped": "Stop the server to manage plugins.",
  // Table columns.
  "plugins.col.name": "Name",
  "plugins.col.version": "Version",
  "plugins.col.source": "Source",
  "plugins.col.side": "Side",
  "plugins.col.status": "Status",
  "plugins.col.size": "Size",
  "plugins.col.actions": "Actions",
  // Status / source labels.
  "plugins.status.enabled": "Enabled",
  "plugins.status.disabled": "Disabled",
  "plugins.source.local": "Local",
  "plugins.source.modrinth": "Modrinth",
  // Side (server / client / both, issue #1308).
  "plugins.side.label": "Side",
  "plugins.side.server": "Server",
  "plugins.side.client": "Client",
  "plugins.side.both": "Both",
  // Actions.
  "plugins.enable": "Enable",
  "plugins.disable": "Disable",
  "plugins.remove": "Remove",
  "plugins.update": "Update",
  "plugins.install": "Upload JAR",
  "plugins.browse": "Browse Modrinth",
  "plugins.downloadClientModpack": "Download client modpack",
  // Update indicators.
  "plugins.updateAvailable": "Update available: ",
  // Remove dialog.
  "plugins.removeDialog.title": "Remove plugin",
  "plugins.removeDialog.body":
    "This permanently removes the plugin file from the server. This cannot be undone.",
  "plugins.removeDialog.confirm": "Remove plugin",
  "plugins.removeDialog.prompt": "Type REMOVE to confirm",
  "plugins.removeDialog.phrase": "REMOVE",
  // Dependencies.
  "plugins.dependencies": "Dependencies",
  "plugins.dependencies.loading": "Loading dependencies…",
  "plugins.dependencies.empty": "No dependencies.",
  "plugins.dependencies.required": "required",
  "plugins.dependencies.optional": "optional",
  "plugins.dependencies.installed": "installed",
  "plugins.dependencies.missing": "missing",
  // Modrinth search modal.
  "plugins.search.title": "Browse Modrinth",
  "plugins.search.placeholder": "Search plugins or mods…",
  "plugins.search.empty": "No results.",
  "plugins.search.downloads": "downloads",
  "plugins.search.by": "by",
  "plugins.search.install": "Install",
  "plugins.search.installing": "Installing…",
  "plugins.search.versions": "Versions",
  "plugins.search.back": "Back to search",
  // Outcomes (toasts).
  "plugins.enabled": "Plugin enabled.",
  "plugins.disabled": "Plugin disabled.",
  "plugins.removed": "Plugin removed.",
  "plugins.updated": "Plugin updated.",
  "plugins.installed": "Plugin installed.",
  "plugins.sideUpdated": "Plugin side updated.",
  "plugins.catalogInstalled": "Plugin installed from Modrinth.",
  "plugins.error.notStopped": "Stop the server before managing plugins.",
  "plugins.error.generic": "Something went wrong. Please try again.",
  // Dependency / compatibility validation checklist (issue #1307).
  "plugins.validation.heading": "Dependencies & compatibility",
  "plugins.validation.ok": "No issues found.",
  "plugins.validation.missingDep":
    "{mod} requires {dependency} ({range}), which is not installed.",
  "plugins.validation.versionUnsatisfied":
    "{mod} requires {dependency} ({range}), but the installed {present} does not satisfy it.",
  "plugins.validation.conflict": "{mod} conflicts with {other}.",
  "plugins.validation.mcMismatch":
    "{mod} does not list MC {serverVersion} (supports {modVersions}).",
  // Permission family and code labels for the role/grant matrix.
  "communitySettings.roles.family.plugin": "Plugins",
  "communitySettings.roles.code.plugin:read": "Read",
  "communitySettings.roles.code.plugin:manage": "Manage",
  // Audit operation labels for plugin actions.
  "communitySettings.audit.op.plugin:install": "Install plugin",
  "communitySettings.audit.op.plugin:remove": "Remove plugin",
  "communitySettings.audit.op.plugin:enable": "Enable plugin",
  "communitySettings.audit.op.plugin:disable": "Disable plugin",
  "communitySettings.audit.op.plugin:update": "Update plugin",
  "communitySettings.audit.targetType.plugin": "Plugin",

  // Resource pack library (issue #1178). One contiguous block to keep merge
  // conflicts with sibling i18n PRs minimal.
  "nav.resourcePacks": "Resource packs",
  "page.resourcePacks": "Resource packs",
  "resourcePacks.subtitle":
    "Upload and manage resource packs for use with Minecraft servers.",
  "resourcePacks.loading": "Loading resource packs…",
  "resourcePacks.loadError": "Could not load resource packs.",
  "resourcePacks.empty": "No resource packs yet.",
  "resourcePacks.upload": "Upload pack",
  "resourcePacks.col.displayName": "Name",
  "resourcePacks.col.filename": "Filename",
  "resourcePacks.col.size": "Size",
  "resourcePacks.col.sha1": "SHA-1",
  "resourcePacks.col.uploaded": "Uploaded",
  "resourcePacks.col.uploader": "Uploader",
  "resourcePacks.download": "Download",
  "resourcePacks.delete": "Delete",
  "resourcePacks.uploadDialog.title": "Upload resource pack",
  "resourcePacks.uploadDialog.displayName": "Display name",
  "resourcePacks.uploadDialog.file": "File (.zip)",
  "resourcePacks.uploadDialog.submit": "Upload",
  "resourcePacks.uploadDialog.uploading": "Uploading…",
  "resourcePacks.uploadDialog.nameRequired": "Enter a display name.",
  "resourcePacks.uploadDialog.fileRequired": "Choose a .zip file.",
  "resourcePacks.uploaded": "Resource pack uploaded.",
  "resourcePacks.deleted": "Resource pack deleted.",
  "resourcePacks.deleteDialog.title": "Delete resource pack",
  "resourcePacks.deleteDialog.body":
    "This permanently deletes the resource pack. Packs currently assigned to servers cannot be deleted.",
  "resourcePacks.deleteDialog.confirm": "Delete pack",
  "resourcePacks.deleteDialog.prompt":
    "Type the display name to enable deletion",
  "resourcePacks.error.tooLarge": "That file exceeds the 256 MiB upload limit.",
  "resourcePacks.error.uploadFailed": "Could not upload the resource pack.",
  "resourcePacks.error.deleteFailed": "Could not delete the resource pack.",
  "resourcePacks.error.inUse":
    "This resource pack is assigned to one or more servers and cannot be deleted.",
  "resourcePacks.error.downloadFailed": "Could not download the resource pack.",

  // Server resource pack assignment (issue #1179). Displayed as a card in the
  // server detail Settings tab.
  "serverDetail.resourcePack.heading": "Resource pack",
  "serverDetail.resourcePack.none": "No resource pack assigned.",
  "serverDetail.resourcePack.assign": "Assign",
  "serverDetail.resourcePack.change": "Change",
  "serverDetail.resourcePack.remove": "Remove",
  "serverDetail.resourcePack.name": "Name",
  "serverDetail.resourcePack.filename": "Filename",
  "serverDetail.resourcePack.size": "Size",
  "serverDetail.resourcePack.sha1": "SHA-1",
  "serverDetail.resourcePack.url": "Public URL",
  "serverDetail.resourcePack.urlCopied": "Copied!",
  "serverDetail.resourcePack.required": "Required",
  "serverDetail.resourcePack.notRequired": "Optional",
  "serverDetail.resourcePack.prompt": "Prompt",
  "serverDetail.resourcePack.promptNone": "None",
  "serverDetail.resourcePack.notAtRest":
    "Stop the server to change resource pack settings.",
  "serverDetail.resourcePack.assigned": "Resource pack assigned.",
  "serverDetail.resourcePack.unassigned": "Resource pack unassigned.",
  "serverDetail.resourcePack.assignError":
    "Could not assign the resource pack.",
  "serverDetail.resourcePack.unassignError":
    "Could not unassign the resource pack.",
  "serverDetail.resourcePack.assignDialog.title": "Assign resource pack",
  "serverDetail.resourcePack.assignDialog.select": "Resource pack",
  "serverDetail.resourcePack.assignDialog.selectPlaceholder": "Select a pack…",
  "serverDetail.resourcePack.assignDialog.require": "Require resource pack",
  "serverDetail.resourcePack.assignDialog.prompt":
    "Custom prompt (shown to players)",
  "serverDetail.resourcePack.assignDialog.submit": "Assign",
  "serverDetail.resourcePack.assignDialog.loading": "Loading packs…",
  "serverDetail.resourcePack.assignDialog.empty": "No packs available.",
  "serverDetail.resourcePack.removeDialog.title": "Remove resource pack",
  "serverDetail.resourcePack.removeDialog.body":
    "Remove the resource pack assignment from this server?",
  "serverDetail.resourcePack.removeDialog.confirm": "Remove",

  // Permission / authorization feedback (WEBUI_SPEC.md 7.3 / 7.4)
  "permissions.denied": "You do not have permission to do that.",
  // Composed with the missing permission code, e.g. "You lack: server:start".
  "permissions.deniedNamed": "You lack: ",

  // Error boundary (#1211): shown when an unhandled rendering error crashes a
  // component subtree instead of the default white-screen unmount.
  "errorBoundary.title": "Something went wrong",
  "errorBoundary.body":
    "An unexpected error occurred. Reloading usually fixes it.",
  "errorBoundary.reload": "Reload page",

  // Shared format strings — heartbeat age (#1214)
  "format.secondsAgo": "{value}s ago",
  "format.minutesAgo": "{value}m ago",
  "format.hoursAgo": "{value}h ago",
} as const;
