import { Navigate, Route, Routes } from "react-router";
import { ToastProvider } from "./components/Toast.tsx";
import { AuthPage } from "./pages/AuthPage.tsx";
import { DashboardPage } from "./pages/DashboardPage.tsx";
import { PlaceholderPage } from "./pages/PlaceholderPage.tsx";
import { AppShell } from "./shell/AppShell.tsx";

// Routing skeleton mirroring the screen map (WEBUI_SPEC.md Section 5). Auth
// pages render outside the shell chrome; everything else nests under AppShell.
// No auth guards or data fetching yet (Phase 1) — each route is a placeholder.
export function App() {
  return (
    <ToastProvider>
      <Routes>
        <Route
          path="/login"
          element={
            <AuthPage
              titleKey="page.login"
              altKey="auth.toRegister"
              altTo="/register"
            />
          }
        />
        <Route
          path="/register"
          element={
            <AuthPage
              titleKey="page.register"
              altKey="auth.toLogin"
              altTo="/login"
            />
          }
        />

        <Route element={<AppShell />}>
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
          <Route
            path="/account"
            element={<PlaceholderPage titleKey="page.account" />}
          />

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

        <Route path="/" element={<Navigate to="/login" replace />} />
        <Route
          path="*"
          element={<PlaceholderPage titleKey="page.notFound" />}
        />
      </Routes>
    </ToastProvider>
  );
}
