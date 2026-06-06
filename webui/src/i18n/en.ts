// English dictionary (shipped). A second locale (e.g. Japanese) can be added as
// a sibling object with the same keys; see WEBUI_SPEC.md Section 7.5.
export const en = {
  "app.title": "mc-server-dashboard",

  // Shell chrome
  "shell.brand": "MC Dashboard",
  "shell.account": "Account",

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

  // UX primitives (WEBUI_SPEC.md Section 7.4)
  "common.cancel": "Cancel",
  "common.close": "Close",

  // Primitives demonstration (placeholder dashboard)
  "demo.showToast": "Show success toast",
  "demo.showError": "Show error toast",
  "demo.openModal": "Open modal",
  "demo.deleteServer": "Delete server",
  "demo.toastSuccess": "Action completed.",
  "demo.toastError": "Something went wrong.",
  "demo.modalTitle": "Example modal",
  "demo.modalBody": "Modals overlay the shell and stay until dismissed.",
  "demo.confirmTitle": "Delete server",
  "demo.confirmBody":
    "This permanently deletes the server and its data. Type the server name to confirm.",
  "demo.confirmServerName": "survival",
  "demo.confirmPrompt": "Type the server name to enable deletion",
  "demo.deleted": "Server deleted.",

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
} as const;
