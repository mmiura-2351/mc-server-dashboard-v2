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
} as const;
