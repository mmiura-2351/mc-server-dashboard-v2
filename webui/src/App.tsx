import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import { useSession } from "./auth/SessionProvider.tsx";
import { ToastProvider } from "./components/Toast.tsx";
import { t } from "./i18n/index.ts";
import { AccountPage } from "./pages/AccountPage.tsx";
import { DashboardPage } from "./pages/DashboardPage.tsx";
import { LoginPage } from "./pages/LoginPage.tsx";
import { PlaceholderPage } from "./pages/PlaceholderPage.tsx";
import { RegisterPage } from "./pages/RegisterPage.tsx";
import { ServerCreatePage } from "./pages/ServerCreatePage.tsx";
import { ServerDetailPage } from "./pages/ServerDetailPage.tsx";
import { useActiveCommunity } from "./permissions/ActiveCommunityProvider.tsx";
import { dashboardPath, postLoginPath } from "./routes.ts";
import { AppShell } from "./shell/AppShell.tsx";

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
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-in") {
    return <Navigate to={postLoginPath(from)} replace />;
  }
  return <>{children}</>;
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
  return <PlaceholderPage titleKey="page.dashboard" />;
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
            element={<PlaceholderPage titleKey="page.communitySettings" />}
          />
          <Route path="/account" element={<AccountPage />} />

          <Route
            path="/admin"
            element={<PlaceholderPage titleKey="page.adminOverview" />}
          />
          <Route
            path="/admin/users"
            element={<PlaceholderPage titleKey="page.adminUsers" />}
          />
          <Route
            path="/admin/communities"
            element={<PlaceholderPage titleKey="page.adminCommunities" />}
          />
          <Route
            path="/admin/workers"
            element={<PlaceholderPage titleKey="page.adminWorkers" />}
          />
          <Route
            path="/admin/versions"
            element={<PlaceholderPage titleKey="page.adminVersions" />}
          />
          <Route
            path="/admin/audit"
            element={<PlaceholderPage titleKey="page.adminAudit" />}
          />
        </Route>

        <Route
          path="*"
          element={<PlaceholderPage titleKey="page.notFound" />}
        />
      </Routes>
    </ToastProvider>
  );
}
