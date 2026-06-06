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
  "common.confirm": "Confirm",
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
