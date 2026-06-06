import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router";
import { useSession } from "./auth/SessionProvider.tsx";
import { ToastProvider } from "./components/Toast.tsx";
import { t } from "./i18n/index.ts";
import { AccountPage } from "./pages/AccountPage.tsx";
import { DashboardPage } from "./pages/DashboardPage.tsx";
import { LoginPage } from "./pages/LoginPage.tsx";
import { PlaceholderPage } from "./pages/PlaceholderPage.tsx";
import { RegisterPage } from "./pages/RegisterPage.tsx";
import { DASHBOARD_PATH } from "./routes.ts";
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
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-out") {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

// Auth routes are for signed-out users: while bootstrapping show the loading
// state, signed-in users are redirected to the dashboard.
function RequireAnon({ children }: { children: ReactNode }) {
  const { status } = useSession();
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-in") {
    return <Navigate to={DASHBOARD_PATH} replace />;
  }
  return <>{children}</>;
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
          <Route path="/communities/:cid" element={<DashboardPage />} />
          <Route
            path="/communities/:cid/servers/new"
            element={<PlaceholderPage titleKey="page.serverCreate" />}
          />
          <Route
            path="/communities/:cid/servers/:sid"
            element={<PlaceholderPage titleKey="page.serverDetail" />}
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

        <Route path="/" element={<Navigate to={DASHBOARD_PATH} replace />} />
        <Route
          path="*"
          element={<PlaceholderPage titleKey="page.notFound" />}
        />
      </Routes>
    </ToastProvider>
  );
}
