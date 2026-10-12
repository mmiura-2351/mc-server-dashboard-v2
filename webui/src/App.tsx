import type { ReactNode } from "react";
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
import { ErrorBoundary } from "./components/ErrorBoundary.tsx";
import { ToastProvider } from "./components/Toast.tsx";
import { t } from "./i18n/index.ts";
import { lazyRetry } from "./lazyRetry.ts";
import { LoginPage } from "./pages/LoginPage.tsx";
import { NoCommunityPage } from "./pages/NoCommunityPage.tsx";
import { NotFoundPage } from "./pages/NotFoundPage.tsx";
import { RegisterPage } from "./pages/RegisterPage.tsx";
import { useActiveCommunity } from "./permissions/ActiveCommunityProvider.tsx";
import { dashboardPath, postLoginPath, safeNextPath } from "./routes.ts";
import { AppShell } from "./shell/AppShell.tsx";

// Load authenticated pages on demand; keep auth and fallback pages eager to avoid first-paint suspension.
// The lazy loaders adapt named exports to React.lazy's default-export contract.
const AccountPage = lazyRetry(() =>
  import("./pages/AccountPage.tsx").then((m) => ({ default: m.AccountPage })),
);
const AdminAuditPage = lazyRetry(() =>
  import("./pages/AdminAuditPage.tsx").then((m) => ({
    default: m.AdminAuditPage,
  })),
);
const AdminCommunitiesPage = lazyRetry(() =>
  import("./pages/AdminCommunitiesPage.tsx").then((m) => ({
    default: m.AdminCommunitiesPage,
  })),
);
const AdminOverviewPage = lazyRetry(() =>
  import("./pages/AdminOverviewPage.tsx").then((m) => ({
    default: m.AdminOverviewPage,
  })),
);
const AdminUsersPage = lazyRetry(() =>
  import("./pages/AdminUsersPage.tsx").then((m) => ({
    default: m.AdminUsersPage,
  })),
);
const AdminVersionsPage = lazyRetry(() =>
  import("./pages/AdminVersionsPage.tsx").then((m) => ({
    default: m.AdminVersionsPage,
  })),
);
const AdminWorkersPage = lazyRetry(() =>
  import("./pages/AdminWorkersPage.tsx").then((m) => ({
    default: m.AdminWorkersPage,
  })),
);
const CommunitySettingsPage = lazyRetry(() =>
  import("./pages/CommunitySettingsPage.tsx").then((m) => ({
    default: m.CommunitySettingsPage,
  })),
);
const DashboardPage = lazyRetry(() =>
  import("./pages/DashboardPage.tsx").then((m) => ({
    default: m.DashboardPage,
  })),
);
const ServerCreatePage = lazyRetry(() =>
  import("./pages/ServerCreatePage.tsx").then((m) => ({
    default: m.ServerCreatePage,
  })),
);
const ResourcePacksPage = lazyRetry(() =>
  import("./pages/ResourcePacksPage.tsx").then((m) => ({
    default: m.ResourcePacksPage,
  })),
);
const ServerDetailPage = lazyRetry(() =>
  import("./pages/ServerDetailPage.tsx").then((m) => ({
    default: m.ServerDetailPage,
  })),
);

// Hold guards during cookie restoration to avoid briefly redirecting returning users to /login.
function SessionLoading() {
  return (
    <div className="auth-wrap" role="status">
      {t("auth.loading")}
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const location = useLocation();
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-out") {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

// Honor the saved deep link when sign-in finishes under the anonymous-route guard.
function RequireAnon({ children }: { children: ReactNode }) {
  const { status } = useSession();
  const from = (useLocation().state as { from?: unknown } | null)?.from;
  const [searchParams] = useSearchParams();
  if (status === "bootstrapping") {
    return <SessionLoading />;
  }
  if (status === "signed-in") {
    // The validated expiry return target takes precedence over the guard's saved location.
    const next = safeNextPath(searchParams.get("next"));
    return <Navigate to={next ?? postLoginPath(from)} replace />;
  }
  return <>{children}</>;
}

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

function Landing() {
  const {
    communityId,
    communities,
    communitiesError,
    communitiesFetching,
    refetchCommunities,
  } = useActiveCommunity();
  if (communityId !== null) {
    return <Navigate to={dashboardPath(communityId)} replace />;
  }
  if (communitiesError && communities === undefined) {
    return (
      <div className="auth-wrap" role="alert">
        <div style={{ textAlign: "center" }}>
          <p>{t("shell.communitiesError")}</p>
          <button
            type="button"
            onClick={refetchCommunities}
            disabled={communitiesFetching}
          >
            {t("shell.communitiesRetry")}
          </button>
        </div>
      </div>
    );
  }
  if (communities === undefined) {
    return <SessionLoading />;
  }
  return <NoCommunityPage />;
}

export function App() {
  return (
    <ErrorBoundary>
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
    </ErrorBoundary>
  );
}
