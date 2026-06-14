import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { shortId } from "../format.ts";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { dashboardPath } from "../routes.ts";
import { lifecycleErrorMessage } from "./lifecycleErrors.ts";
import {
  actionApplies,
  normalizeState,
  type ObservedState,
  statePill,
} from "./serverState.ts";
import { serversKey, useCommunityEvents } from "./useCommunityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type LifecycleAction = "start" | "stop" | "restart";

// Copy text to clipboard with an execCommand fallback for insecure contexts.
function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) {
        resolve();
      } else {
        reject();
      }
    } catch {
      reject();
    }
  });
}

// Dashboard server-list layout. Cards remain the default (#541); the table view
// is the compact alternative for many servers / narrow screens.
type ViewMode = "cards" | "table";
const VIEW_MODE_KEY = "mcsd.dashboard.viewMode";

// Persist the chosen view across reloads, mirroring the column-width persistence
// in ResizableColumns (best-effort localStorage, never break on corrupt/blocked
// storage; fall back to the card default).
function loadViewMode(): ViewMode {
  try {
    return localStorage.getItem(VIEW_MODE_KEY) === "table" ? "table" : "cards";
  } catch {
    return "cards";
  }
}

function saveViewMode(mode: ViewMode): void {
  try {
    localStorage.setItem(VIEW_MODE_KEY, mode);
  } catch {
    // Best-effort persistence; ignore quota/availability failures.
  }
}

function useViewMode(): [ViewMode, (mode: ViewMode) => void] {
  const [mode, setMode] = useState<ViewMode>(() => loadViewMode());
  const select = (next: ViewMode) => {
    setMode(next);
    saveViewMode(next);
  };
  return [mode, select];
}

export function DashboardPage() {
  // The community to list is the URL `:cid` (#784) — not the active community,
  // which can disagree with the URL on a stale bookmark or a community the user
  // has left. The active-community list (the caller's membership) is only used
  // to confirm the URL cid is one the caller belongs to.
  const { cid } = useParams();
  const { communities } = useActiveCommunity();

  // Membership still loading: hold the chrome rather than flash a not-found.
  if (communities === undefined) {
    return (
      <DashboardChrome>
        <p className="sub">{t("dashboard.loading")}</p>
      </DashboardChrome>
    );
  }
  // The URL names a community the caller is not a member of (or no cid at all):
  // show a clear not-found state instead of silently listing another community.
  if (cid === undefined || !communities.some((c) => c.id === cid)) {
    return (
      <DashboardChrome>
        <div className="empty">
          <div className="big">{t("community.notFound.title")}</div>
          <p className="sub">{t("community.notFound.body")}</p>
        </div>
      </DashboardChrome>
    );
  }
  return <Loaded communityId={cid} />;
}

function DashboardChrome({
  children,
  degraded = false,
  toolbar,
}: {
  children: ReactNode;
  degraded?: boolean;
  toolbar?: ReactNode;
}) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("page.dashboard")}</h1>
          <div className="sub">{t("dashboard.subtitle")}</div>
        </div>
        {degraded && (
          <span className="pill live-degraded" role="status">
            {t("dashboard.liveDegraded")}
          </span>
        )}
        {toolbar && <div className="actions">{toolbar}</div>}
      </div>
      {children}
    </>
  );
}

// Card/table view switch (#541), persisted in localStorage. A segmented control
// with `aria-pressed` so assistive tech reports the active view.
function ViewToggle({
  view,
  onSelect,
}: {
  view: ViewMode;
  onSelect: (view: ViewMode) => void;
}) {
  return (
    <fieldset className="view-toggle" aria-label={t("dashboard.view.label")}>
      <button
        type="button"
        className="btn sm"
        aria-pressed={view === "cards"}
        onClick={() => onSelect("cards")}
      >
        {t("dashboard.view.cards")}
      </button>
      <button
        type="button"
        className="btn sm"
        aria-pressed={view === "table"}
        onClick={() => onSelect("table")}
      >
        {t("dashboard.view.table")}
      </button>
    </fieldset>
  );
}

function Loaded({ communityId }: { communityId: string }) {
  const can = useCan();
  const degraded = useCommunityEvents(communityId);
  const [view, setView] = useViewMode();
  const query = useQuery({
    queryKey: serversKey(communityId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers", {
          community_id: communityId,
        }),
      ),
  });

  if (query.isPending) {
    return (
      <DashboardChrome>
        <p className="sub">{t("dashboard.loading")}</p>
      </DashboardChrome>
    );
  }
  if (query.isError || query.data === undefined) {
    return (
      <DashboardChrome>
        <p className="field-error">{t("dashboard.loadError")}</p>
      </DashboardChrome>
    );
  }

  const servers = query.data;
  if (servers.length === 0) {
    return (
      <DashboardChrome degraded={degraded}>
        <EmptyState communityId={communityId} />
      </DashboardChrome>
    );
  }

  return (
    <DashboardChrome
      degraded={degraded}
      toolbar={<ViewToggle view={view} onSelect={setView} />}
    >
      {view === "table" ? (
        <ServerTable servers={servers} communityId={communityId} can={can} />
      ) : (
        <div className="grid cols-2">
          {servers.map((server) => (
            <ServerCard
              key={server.id}
              server={server}
              communityId={communityId}
              can={can}
            />
          ))}
        </div>
      )}
    </DashboardChrome>
  );
}

function EmptyState({ communityId }: { communityId: string }) {
  return (
    <div className="empty">
      <div className="big">{t("dashboard.empty")}</div>
      <p className="sub">{t("dashboard.emptyHint")}</p>
      <Link
        className="btn primary"
        to={`${dashboardPath(communityId)}/servers/new`}
      >
        {t("dashboard.createServer")}
      </Link>
    </div>
  );
}

// The per-server lifecycle mutation plus the observed/optimistic state, shared by
// the card and table rows so neither duplicates the start/stop/restart business
// logic (#541).
function useLifecycle(server: ServerResponse, communityId: string) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (action: LifecycleAction) =>
      api.post(
        apiPath(
          `/api/communities/{community_id}/servers/{server_id}/${action}`,
          {
            community_id: communityId,
            server_id: server.id,
          },
        ),
      ),
    // Optimistically patch the server's observed_state in the list cache so
    // the pill transitions instantly, before the API responds (#1071).
    onMutate: (action: LifecycleAction) => {
      const key = serversKey(communityId);
      const previous = queryClient.getQueryData<ServerResponse[]>(key);
      queryClient.setQueryData<ServerResponse[]>(key, (old) =>
        old?.map((s) =>
          s.id === server.id
            ? { ...s, observed_state: requestedState(action) }
            : s,
        ),
      );
      return { previous };
    },
    // Always re-fetch the list once the request settles (no polling loop here).
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
    },
    onError: (error, _action, context) => {
      // Rollback the optimistic cache update before showing the error toast.
      if (context?.previous) {
        queryClient.setQueryData(serversKey(communityId), context.previous);
      }
      // 403 → the permission glue (toast + capability refetch). Everything
      // else → the shared lifecycle mapping: known non-race 409 reasons get a
      // specific toast, other 409s the "state changed — refresh" treatment
      // (SPEC 7.4; the refetch already runs in onSettled), the rest a generic
      // toast.
      if (onForbidden(error)) {
        return;
      }
      showToast(t(lifecycleErrorMessage(error)), "error");
    },
  });

  const state = normalizeState(server.observed_state);
  // While a request is in flight, show the transition it requests so the pill
  // moves immediately, before the list refetch returns.
  const displayState: ObservedState = mutation.isPending
    ? requestedState(mutation.variables)
    : state;

  return { mutation, state, displayState };
}

// The state pill for an observed/optimistic state.
function StatePill({ state }: { state: ObservedState }) {
  const pill = statePill(state);
  return (
    <span className={`pill ${pill.className}${pill.blink ? " blink" : ""}`}>
      {t(pill.labelKey)}
    </span>
  );
}

interface ServerRowProps {
  server: ServerResponse;
  communityId: string;
  can: Can;
}

function ServerCard({ server, communityId, can }: ServerRowProps) {
  const { mutation, state, displayState } = useLifecycle(server, communityId);
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopy = useCallback(() => {
    if (server.join_hostname === null) return;
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    copyToClipboard(server.join_hostname).then(
      () => {
        setCopied(true);
        copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
      },
      () => {
        setCopied(false);
      },
    );
  }, [server.join_hostname]);

  return (
    <div className="card server-card">
      <div className="head">
        <span className="name">
          <Link to={`${dashboardPath(communityId)}/servers/${server.id}`}>
            {server.name}
          </Link>
        </span>
        <StatePill state={displayState} />
      </div>
      <div className="meta">
        <span className="badge type">
          {server.server_type} {server.mc_version}
        </span>
        <span className="badge">{server.execution_backend}</span>
        {server.join_hostname !== null ? (
          <button
            type="button"
            className="badge copyable"
            title={server.join_hostname}
            onClick={handleCopy}
          >
            {copied ? t("dashboard.copiedJoinHostname") : server.join_hostname}
          </button>
        ) : (
          server.game_port !== null && (
            <span className="badge">:{server.game_port}</span>
          )
        )}
      </div>
      <div className="foot">
        {(["start", "stop", "restart"] as const).map((action) => (
          <Action
            key={action}
            action={action}
            server={server}
            state={state}
            pending={mutation.isPending}
            can={can}
            onRun={mutation.mutate}
          />
        ))}
        <span className="right" title={server.assigned_worker_id ?? undefined}>
          {server.assigned_worker_id !== null
            ? `${t("dashboard.col.worker")}: ${shortId(server.assigned_worker_id)}`
            : t("dashboard.noWorker")}
        </span>
      </div>
    </div>
  );
}

// Compact table view (#541): the same servers/data as the cards, reusing the
// ResizableTable column affordances and the shared serverState helpers.
function ServerTable({
  servers,
  communityId,
  can,
}: {
  servers: ServerResponse[];
  communityId: string;
  can: Can;
}) {
  return (
    <div className="card dashboard-table" style={{ padding: 0 }}>
      <ResizableTable storageKey="mcsd.colw.dashboard-servers" className="data">
        <thead>
          <tr>
            <th>{t("dashboard.col.name")}</th>
            <th>{t("dashboard.col.state")}</th>
            <th>{t("dashboard.col.type")}</th>
            <th>{t("dashboard.col.backend")}</th>
            <th>{t("dashboard.col.address")}</th>
            <th>{t("dashboard.col.worker")}</th>
            <th>{t("dashboard.col.actions")}</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((server) => (
            <ServerRow
              key={server.id}
              server={server}
              communityId={communityId}
              can={can}
            />
          ))}
        </tbody>
      </ResizableTable>
    </div>
  );
}

function ServerRow({ server, communityId, can }: ServerRowProps) {
  const { mutation, state, displayState } = useLifecycle(server, communityId);
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopy = useCallback(() => {
    if (server.join_hostname === null) return;
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    copyToClipboard(server.join_hostname).then(
      () => {
        setCopied(true);
        copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
      },
      () => {
        setCopied(false);
      },
    );
  }, [server.join_hostname]);

  return (
    <tr>
      <td>
        <Link to={`${dashboardPath(communityId)}/servers/${server.id}`}>
          {server.name}
        </Link>
      </td>
      <td>
        <StatePill state={displayState} />
      </td>
      <td>
        {server.server_type} {server.mc_version}
      </td>
      <td>{server.execution_backend}</td>
      <td>
        {server.join_hostname !== null ? (
          <button
            type="button"
            className="copyable"
            title={server.join_hostname}
            style={{
              cursor: "pointer",
              background: "none",
              border: "none",
              padding: 0,
              font: "inherit",
              color: "inherit",
            }}
            onClick={handleCopy}
          >
            {copied ? t("dashboard.copiedJoinHostname") : server.join_hostname}
          </button>
        ) : (
          (server.game_port ?? "—")
        )}
      </td>
      <td className="dim" title={server.assigned_worker_id ?? undefined}>
        {server.assigned_worker_id !== null
          ? shortId(server.assigned_worker_id)
          : t("dashboard.noWorker")}
      </td>
      <td className="row-actions">
        {(["start", "stop", "restart"] as const).map((action) => (
          <Action
            key={action}
            action={action}
            server={server}
            state={state}
            pending={mutation.isPending}
            can={can}
            onRun={mutation.mutate}
          />
        ))}
      </td>
    </tr>
  );
}

interface ActionProps {
  action: LifecycleAction;
  server: ServerResponse;
  state: ObservedState;
  pending: boolean;
  can: Can;
  onRun: (action: LifecycleAction) => void;
}

// One lifecycle button. Rendered only when permitted (controls render from
// permissions, WEBUI_SPEC.md 7.3); disabled while a request is in flight or the
// action does not apply to the current state.
function Action({ action, server, state, pending, can, onRun }: ActionProps) {
  if (!can(`server:${action}`, { serverId: server.id })) {
    return null;
  }
  const disabled = pending || !actionApplies(action, state);
  return (
    <button
      type="button"
      className={`btn sm${action === "start" ? " success" : ""}`}
      disabled={disabled}
      onClick={() => onRun(action)}
    >
      {t(LABEL_KEY[action])}
    </button>
  );
}

const LABEL_KEY = {
  start: "dashboard.start",
  stop: "dashboard.stop",
  restart: "dashboard.restart",
} as const;

// The requested target state for an in-flight action, used for the optimistic
// transitional pill.
function requestedState(action: LifecycleAction | undefined): ObservedState {
  if (action === "stop") {
    return "stopping";
  }
  if (action === "restart") {
    return "restarting";
  }
  return "starting";
}
