import { lazy, type ReactNode } from "react";
import {
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useSearchParams,
} from "react-router";
import { useSession } from "./auth/SessionProvider.tsx";
import { useCurrentUser } from "./auth/useCurrentUser.ts";
import { ToastProvider } from "./components/Toast.tsx";
import { t } from "./i18n/index.ts";
import { LoginPage } from "./pages/LoginPage.tsx";
import { NoCommunityPage } from "./pages/NoCommunityPage.tsx";
import { NotFoundPage } from "./pages/NotFoundPage.tsx";
import { RegisterPage } from "./pages/RegisterPage.tsx";
import { useActiveCommunity } from "./permissions/ActiveCommunityProvider.tsx";
import { dashboardPath, postLoginPath, safeNextPath } from "./routes.ts";
import { AppShell } from "./shell/AppShell.tsx";

// Route-level code splitting (#553): the heavier authenticated pages (dashboard,
// server detail/create, community settings, the admin area, account) load on
// demand so the SPA no longer ships every page in the initial chunk. React.lazy
// needs a default export; these pages are named exports, so each loader re-maps
// the named export to `default`. The eagerly imported pages above stay in the
// initial chunk on purpose: the auth pages (Login/Register) and NotFoundPage
// are first paint / fallback chrome, and NoCommunityPage renders inline from the
// Landing redirect — splitting them would only add a Suspense flash before the
// very first screen.
const AccountPage = lazy(() =>
  import("./pages/AccountPage.tsx").then((m) => ({ default: m.AccountPage })),
);
const AdminAuditPage = lazy(() =>
  import("./pages/AdminAuditPage.tsx").then((m) => ({
    default: m.AdminAuditPage,
  })),
);
const AdminCommunitiesPage = lazy(() =>
  import("./pages/AdminCommunitiesPage.tsx").then((m) => ({
    default: m.AdminCommunitiesPage,
  })),
);
const AdminOverviewPage = lazy(() =>
  import("./pages/AdminOverviewPage.tsx").then((m) => ({
    default: m.AdminOverviewPage,
  })),
);
const AdminUsersPage = lazy(() =>
  import("./pages/AdminUsersPage.tsx").then((m) => ({
    default: m.AdminUsersPage,
  })),
);
const AdminVersionsPage = lazy(() =>
  import("./pages/AdminVersionsPage.tsx").then((m) => ({
    default: m.AdminVersionsPage,
  })),
);
const AdminWorkersPage = lazy(() =>
  import("./pages/AdminWorkersPage.tsx").then((m) => ({
    default: m.AdminWorkersPage,
  })),
);
const CommunitySettingsPage = lazy(() =>
  import("./pages/CommunitySettingsPage.tsx").then((m) => ({
    default: m.CommunitySettingsPage,
  })),
);
const DashboardPage = lazy(() =>
  import("./pages/DashboardPage.tsx").then((m) => ({
    default: m.DashboardPage,
  })),
);
const ServerCreatePage = lazy(() =>
  import("./pages/ServerCreatePage.tsx").then((m) => ({
    default: m.ServerCreatePage,
  })),
);
const ResourcePacksPage = lazy(() =>
  import("./pages/ResourcePacksPage.tsx").then((m) => ({
    default: m.ResourcePacksPage,
  })),
);
const ServerDetailPage = lazy(() =>
  import("./pages/ServerDetailPage.tsx").then((m) => ({
    default: m.ServerDetailPage,
  })),
);

// Neutral loading state shown while the session bootstraps (cookie refresh in
// flight). Guards hold here instead of bouncing a returning user to /login,
// which would flash a redirect before the cookie re-establishes the session.
function SessionLoading() {
  return (
    <div className="auth-wrap" role="status">
      {t("auth.loading")}
    </div>
  );
}

// Shell routes require a session: while bootstrapping show the loading state,
// signed-out users go to /login (WEBUI_SPEC.md 7.1).
function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const location = useLocation();
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-out") {
    // Carry the attempted location so login can return the user there (#424).
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

// Auth routes are for signed-out users: while bootstrapping show the loading
// state, signed-in users are redirected away. When a deep link was stashed in
// router state (#424), honour it here too so a sign-in completing under this
// guard lands on the requested route rather than racing LANDING_PATH.
function RequireAnon({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const from = (useLocation().state as { from?: unknown } | null)?.from;
  const [searchParams] = useSearchParams();
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-in") {
    // A validated `next` (session-expiry return-to, #565) wins over the #424
    // guard stash; both fall back to the post-login landing.
    const next = safeNextPath(searchParams.get("next"));
    return <Navigate to={next ?? postLoginPath(from)} replace />;
  }
  return <>{children}</>;
}

// The platform-admin area (`/admin/*`) is gated on `is_platform_admin` from
// the shared current-user query (WEBUI_SPEC.md Section 3). Rendered as a layout
// route wrapping `<Outlet/>` so each admin page (#475–#479) lands behind this
// guard without touching it. While the user loads we hold on the session
// loading state; non-admins (and a failed load) get a clean denied notice
// rather than a redirect — the server still enforces the truth (FR-AUTHZ-6).
function RequireAdmin() {
  const { data, isPending } = useCurrentUser();
  if (isPending) {
    return <SessionLoading />;
  }
  if (data?.is_platform_admin !== true) {
    return (
      <div className="page-head" role="alert">
        <h1>{t("admin.denied.title")}</h1>
        <div className="sub">{t("admin.denied.body")}</div>
      </div>
    );
  }
  return <Outlet />;
}

// The community-agnostic landing (LANDING_PATH): once the active community
// resolves, redirect to its dashboard. While the community list is loading,
// hold on the session loading state (no flicker); a caller with no communities
// stays here and the shell shows the no-communities hint.
function Landing() {
  const { communityId, communities } = useActiveCommunity();
  if (communityId !== null) {
    return <Navigate to={dashboardPath(communityId)} replace />;
  }
  if (communities === undefined) {
    return <SessionLoading />;
  }
  return <NoCommunityPage />;
}

// Routing mirroring the screen map (WEBUI_SPEC.md Section 5). Auth pages render
// outside the shell chrome; everything else nests under AppShell behind the
// session guard (#410).
export function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route
          path="/login"
          element={
            <RequireAnon>
              <LoginPage />
            </RequireAnon>
          }
        />
        <Route
          path="/register"
          element={
            <RequireAnon>
              <RegisterPage />
            </RequireAnon>
          }
        />

        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route index element={<Landing />} />
          <Route path="/communities/:cid" element={<DashboardPage />} />
          <Route
            path="/communities/:cid/servers/new"
            element={<ServerCreatePage />}
          />
          <Route
            path="/communities/:cid/servers/:sid"
            element={<ServerDetailPage />}
          />
          <Route
            path="/communities/:cid/settings"
            element={<CommunitySettingsPage />}
          />
          <Route path="/account" element={<AccountPage />} />
          <Route path="/resource-packs" element={<ResourcePacksPage />} />

          <Route element={<RequireAdmin />}>
            <Route path="/admin" element={<AdminOverviewPage />} />
            <Route path="/admin/users" element={<AdminUsersPage />} />
            <Route
              path="/admin/communities"
              element={<AdminCommunitiesPage />}
            />
            <Route path="/admin/workers" element={<AdminWorkersPage />} />
            <Route path="/admin/versions" element={<AdminVersionsPage />} />
            <Route path="/admin/audit" element={<AdminAuditPage />} />
          </Route>
        </Route>

        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </ToastProvider>
  );
}
