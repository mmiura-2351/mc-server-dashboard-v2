// The post-sign-in landing route. The dashboard lives under a community scope
// (WEBUI_SPEC.md Section 5); until real community selection lands, the shell
// uses a fixed demo community id (AppShell.tsx), so login / register / the route
// guards all land here. Centralized so they cannot drift apart.
export const DASHBOARD_PATH = "/communities/demo";
