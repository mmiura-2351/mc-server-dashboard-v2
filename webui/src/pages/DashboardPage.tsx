import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { dashboardPath } from "../routes.ts";
import {
  actionApplies,
  normalizeState,
  type ObservedState,
  statePill,
} from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type LifecycleAction = "start" | "stop" | "restart";

// The community-scoped server list (WEBUI_SPEC.md 6.2). REST-only here; live WS
// updates land in the events issue, so there is no polling loop.
function serversKey(communityId: string) {
  return ["communities", communityId, "servers"] as const;
}

export function DashboardPage() {
  const { communityId } = useActiveCommunity();

  // The shell only routes here under an active community, but guard anyway: with
  // no community there is nothing to list.
  if (communityId === null) {
    return (
      <DashboardChrome>
        <p className="sub">{t("shell.noCommunities")}</p>
      </DashboardChrome>
    );
  }
  return <Loaded communityId={communityId} />;
}

function DashboardChrome({ children }: { children: ReactNode }) {
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("page.dashboard")}</h1>
          <div className="sub">{t("dashboard.subtitle")}</div>
        </div>
      </div>
      {children}
    </>
  );
}

function Loaded({ communityId }: { communityId: string }) {
  const can = useCan();
  const query = useQuery({
    queryKey: serversKey(communityId),
    queryFn: () =>
      api.get(
        apiPath("/communities/{community_id}/servers", {
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
      <DashboardChrome>
        <EmptyState communityId={communityId} />
      </DashboardChrome>
    );
  }

  return (
    <DashboardChrome>
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

interface ServerCardProps {
  server: ServerResponse;
  communityId: string;
  can: Can;
}

function ServerCard({ server, communityId, can }: ServerCardProps) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (action: LifecycleAction) =>
      api.post(
        apiPath(`/communities/{community_id}/servers/{server_id}/${action}`, {
          community_id: communityId,
          server_id: server.id,
        }),
      ),
    // Always re-fetch the list once the request settles (no polling loop here).
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
    },
    onError: (error) => {
      // 403 → the permission glue (toast + capability refetch). 409 → a
      // lifecycle race: the state changed under us, so give it the "state
      // changed — refresh" treatment rather than a raw error dump (SPEC 7.4);
      // the refetch already runs in onSettled. Anything else → a generic toast.
      if (onForbidden(error)) {
        return;
      }
      if (error instanceof ApiError && error.status === 409) {
        showToast(t("dashboard.stateChanged"), "error");
        return;
      }
      showToast(t("dashboard.actionFailed"), "error");
    },
  });

  const state = normalizeState(server.observed_state);
  // While a request is in flight, show the transition it requests so the pill
  // moves immediately, before the list refetch returns.
  const displayState: ObservedState = mutation.isPending
    ? requestedState(mutation.variables)
    : state;
  const pill = statePill(displayState);

  return (
    <div className="card server-card">
      <div className="head">
        <span className="name">
          <Link to={`${dashboardPath(communityId)}/servers/${server.id}`}>
            {server.name}
          </Link>
        </span>
        <span className={`pill ${pill.className}${pill.blink ? " blink" : ""}`}>
          {t(pill.labelKey)}
        </span>
      </div>
      <div className="meta">
        <span className="badge type">
          {server.server_type} {server.mc_version}
        </span>
        <span className="badge">{server.execution_backend}</span>
        {server.game_port !== null && (
          <span className="badge">:{server.game_port}</span>
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
        <span className="right">
          {server.assigned_worker_id ?? t("dashboard.noWorker")}
        </span>
      </div>
    </div>
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
