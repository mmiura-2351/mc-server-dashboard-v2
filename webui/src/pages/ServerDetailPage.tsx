import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type KeyboardEvent,
  memo,
  type ReactNode,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { saveUrlAs } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { shortId } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { classifyQueryResult } from "../queryState.ts";
import { dashboardPath } from "../routes.ts";
import { ForgeInstallLog } from "./ForgeInstallLog.tsx";
import { isEulaNotAccepted, lifecycleErrorMessage } from "./lifecycleErrors.ts";
import { stripMinecraftCodes } from "./mcFormat.ts";
import { stampedQueryFn } from "./restReads.ts";
import { ServerAddressBadges } from "./ServerAddressBadges.tsx";
import { ServerBackupsTab } from "./ServerBackupsTab.tsx";
import { ServerFilesTab } from "./ServerFilesTab.tsx";
import { ServerPlayersTab } from "./ServerPlayersTab.tsx";
import { ServerPluginsTab } from "./ServerPluginsTab.tsx";
import { ServerResourcePackSection } from "./ServerResourcePackSection.tsx";
import { ServerSchedulesTab } from "./ServerSchedulesTab.tsx";
import { consoleCommandErrorPresentation } from "./serverConsoleErrorPresentation.ts";
import { serverKey } from "./serverKey.ts";
import { serverSettingsErrorPresentation } from "./serverSettingsErrorPresentation.ts";
import {
  actionApplies,
  atRest,
  normalizeState,
  statePill,
} from "./serverState.ts";
import { handleTabKeyDown, panelId, tabId, useTabHash } from "./urlState.ts";
import { serversKey } from "./useCommunityEvents.ts";
import { useLifecycleMutation } from "./useLifecycleMutation.ts";
import {
  type LogEntry,
  type MetricsSample,
  type ServerEventsState,
  TAIL_LINES,
  useLogs,
  useServerEvents,
} from "./useServerEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

export { serverKey };

const TABS = [
  "overview",
  "console",
  "files",
  "backups",
  "schedules",
  "plugins",
  "players",
  "settings",
] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, TranslationKey> = {
  overview: "serverDetail.tab.overview",
  console: "serverDetail.tab.console",
  files: "serverDetail.tab.files",
  backups: "serverDetail.tab.backups",
  schedules: "serverDetail.tab.schedules",
  plugins: "serverDetail.tab.plugins",
  players: "serverDetail.tab.players",
  settings: "serverDetail.tab.settings",
};

const PLUGIN_UNSUPPORTED_TYPES = new Set(["vanilla"]);

/** Fabric and Forge use mods/; Paper uses plugins/, matching the backend's content mapping. */
const MOD_LOADER_TYPES = new Set(["fabric", "forge"]);

function pluginTabLabelKey(serverType: string): TranslationKey {
  return MOD_LOADER_TYPES.has(serverType)
    ? "serverDetail.tab.mods"
    : "serverDetail.tab.plugins";
}

/** Mint export URLs on click so large ZIPs stream directly to disk without tab buffering. */
function exportViaGrant(communityId: string, server: ServerResponse) {
  return async () => {
    const grant = await api.post(
      apiPath(
        "/api/communities/{community_id}/servers/{server_id}/export/download-grant",
        { community_id: communityId, server_id: server.id },
      ),
    );
    saveUrlAs(grant.download_url, `${server.name}.zip`);
  };
}

/** Make every panel focusable: some states have no focusable child, or begin with noninteractive content. */
function TabPanel({ name, children }: { name: Tab; children: ReactNode }) {
  return (
    <div
      role="tabpanel"
      id={panelId("sd", name)}
      aria-labelledby={tabId("sd", name)}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a focusable tabpanel is the APG tabs pattern itself; the rule has no role exception (eslint-jsx-a11y exempts role="tabpanel" by default).
      tabIndex={0}
    >
      {children}
    </div>
  );
}

export function ServerDetailPage() {
  const { cid, sid } = useParams();
  if (cid === undefined || sid === undefined) {
    return null;
  }
  return <Loaded communityId={cid} serverId={sid} />;
}

function Loaded({
  communityId,
  serverId,
}: {
  communityId: string;
  serverId: string;
}) {
  const can = useCan();
  const [tab, setTab] = useTabHash(TABS);
  // Share one event socket across the open detail page's tabs.
  const events = useServerEvents(communityId, serverId);
  const query = useQuery({
    queryKey: serverKey(communityId, serverId),
    // Stamp REST attempts so live frames can be ordered against them.
    queryFn: stampedQueryFn(({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: serverId,
        }),
        { signal },
      ),
    ),
  });

  const queryState = classifyQueryResult(query);

  if (queryState.kind === "pending") {
    return <p className="sub">{t("serverDetail.loading")}</p>;
  }
  // Retain cached content on background refetch failure; stream status is reported separately.
  if (queryState.kind === "error") {
    return <p className="field-error">{t("serverDetail.loadError")}</p>;
  }

  const server = queryState.data;
  const hidePlugins = PLUGIN_UNSUPPORTED_TYPES.has(server.server_type);
  const visibleTabs = hidePlugins
    ? TABS.filter((name) => name !== "plugins")
    : TABS;
  return (
    <>
      <Header
        server={server}
        communityId={communityId}
        can={can}
        degraded={events.degraded}
        statusDetail={events.statusDetail}
        statusReason={events.statusReason}
        onOpenConsole={() => setTab("console")}
      />
      <div className="tabs" role="tablist">
        {visibleTabs.map((name) => (
          <button
            key={name}
            id={tabId("sd", name)}
            type="button"
            role="tab"
            aria-selected={tab === name}
            aria-controls={panelId("sd", name)}
            tabIndex={tab === name ? 0 : -1}
            className={`tab${tab === name ? " active" : ""}`}
            onClick={() => setTab(name)}
            onKeyDown={(e) =>
              handleTabKeyDown(e, visibleTabs, tab, setTab, "sd")
            }
          >
            {t(
              name === "plugins"
                ? pluginTabLabelKey(server.server_type)
                : TAB_LABEL[name],
            )}
          </button>
        ))}
      </div>
      {tab === "overview" && (
        <TabPanel name="overview">
          <Overview
            server={server}
            events={events}
            onOpenConsole={() => setTab("console")}
          />
        </TabPanel>
      )}
      {tab === "console" && (
        <TabPanel name="console">
          <Console
            server={server}
            communityId={communityId}
            can={can}
            events={events}
          />
        </TabPanel>
      )}
      {tab === "files" && (
        <TabPanel name="files">
          <ServerFilesTab server={server} communityId={communityId} can={can} />
        </TabPanel>
      )}
      {tab === "backups" && (
        <TabPanel name="backups">
          <ServerBackupsTab
            server={server}
            communityId={communityId}
            can={can}
          />
        </TabPanel>
      )}
      {tab === "schedules" && (
        <TabPanel name="schedules">
          <ServerSchedulesTab
            server={server}
            communityId={communityId}
            can={can}
          />
        </TabPanel>
      )}
      {tab === "plugins" && (
        <TabPanel name="plugins">
          <ServerPluginsTab
            server={server}
            communityId={communityId}
            can={can}
          />
        </TabPanel>
      )}
      {tab === "players" && (
        <TabPanel name="players">
          <ServerPlayersTab
            communityId={communityId}
            serverId={server.id}
            can={can}
          />
        </TabPanel>
      )}
      {tab === "settings" && (
        <TabPanel name="settings">
          <Settings server={server} communityId={communityId} can={can} />
        </TabPanel>
      )}
    </>
  );
}

/** Known Forge-install crash reasons can offer an install-log viewer; unknown reasons remain unclassified. */
const FORGE_INSTALL_CRASH_REASONS: ReadonlySet<string> = new Set([
  "forge_install_failed",
  "forge_install_out_of_memory",
  "forge_install_java_incompatible",
]);

const CRASH_REASON_MESSAGE = {
  forge_install_out_of_memory:
    "serverDetail.crashReason.forgeInstallOutOfMemory",
  forge_install_java_incompatible:
    "serverDetail.crashReason.forgeInstallJavaIncompatible",
} as const satisfies Record<string, TranslationKey>;
type SpecificCrashReason = keyof typeof CRASH_REASON_MESSAGE;

function Header({
  server,
  communityId,
  can,
  degraded,
  statusDetail,
  statusReason,
  onOpenConsole,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
  degraded: boolean;
  statusDetail: string;
  statusReason: string;
  onOpenConsole: () => void;
}) {
  const state = normalizeState(server.observed_state);
  const pill = statePill(state);
  const drifting = server.desired_state !== server.observed_state;

  return (
    <div className="page-head">
      <div>
        <div className="breadcrumbs">
          <Link to={dashboardPath(communityId)}>
            {t("serverDetail.breadcrumb")}
          </Link>{" "}
          / {server.name}
        </div>
        <div className="detail-title">
          <h1 className="detail-name">{server.name}</h1>
          <span
            className={`pill ${pill.className}${pill.blink ? " blink" : ""}`}
          >
            {t(pill.labelKey)}
          </span>
          {drifting && (
            <span className="pill settling blink" role="status">
              {t("serverDetail.converging")}
            </span>
          )}
          {degraded && (
            <span className="pill live-degraded" role="status">
              {t("dashboard.liveDegraded")}
            </span>
          )}
        </div>
        {state === "crashed" && (
          <div className="crash-detail">
            {statusDetail.length > 0 && (
              <div>
                <span className="crash-detail-label">
                  {t("serverDetail.crashDetail")}
                </span>{" "}
                {statusDetail}
              </div>
            )}
            {statusReason in CRASH_REASON_MESSAGE && (
              <div>
                {t(CRASH_REASON_MESSAGE[statusReason as SpecificCrashReason])}
              </div>
            )}
            <div>
              {t("serverDetail.crashBanner.guidance")}{" "}
              <button type="button" className="link" onClick={onOpenConsole}>
                {t("serverDetail.crashBanner.viewConsole")}
              </button>
            </div>
            {FORGE_INSTALL_CRASH_REASONS.has(statusReason) &&
              can("file:read", { serverId: server.id }) && (
                <ForgeInstallLog
                  communityId={communityId}
                  serverId={server.id}
                />
              )}
          </div>
        )}
        {state === "unknown" && statusDetail.length > 0 && (
          <div className="crash-detail">
            <span className="crash-detail-label">
              {t("serverDetail.crashDetail")}
            </span>{" "}
            {statusDetail}
          </div>
        )}
        <div className="sub">
          <span className="badge type">
            {server.server_type} {server.mc_version}
          </span>
          <ServerAddressBadges
            server={server}
            buttonClassName="badge copyable"
            fallback={
              <span className="badge">
                {server.game_port !== null
                  ? `:${server.game_port}`
                  : t("serverDetail.noPort")}
              </span>
            }
          />
          <span
            className="badge"
            title={server.assigned_worker_id ?? undefined}
          >
            {server.assigned_worker_id !== null
              ? `${t("serverDetail.worker")}: ${shortId(server.assigned_worker_id)}`
              : t("serverDetail.noWorker")}
          </span>
        </div>
      </div>
      <Controls server={server} communityId={communityId} can={can} />
    </div>
  );
}

function Controls({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const state = normalizeState(server.observed_state);
  const desired = normalizeState(server.desired_state);
  const [eulaOpen, setEulaOpen] = useState(false);

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t(lifecycleErrorMessage(error)), "error");
  };

  const lifecycle = useLifecycleMutation({
    communityId,
    serverId: server.id,
    cacheKey: serverKey(communityId, server.id),
    invalidateKeys: [
      serverKey(communityId, server.id),
      serversKey(communityId),
    ],
    onError: (error, action) => {
      if (onForbidden(error)) {
        return;
      }
      if (isEulaNotAccepted(error)) {
        setEulaOpen(true);
        return;
      }
      showToast(t(lifecycleErrorMessage(error, action)), "error");
    },
  });

  const exportMutation = useMutation({
    mutationFn: exportViaGrant(communityId, server),
    onSuccess: () => showToast(t("serverDetail.exportStarted"), "success"),
    onError,
  });

  const pending = lifecycle.isPending || exportMutation.isPending;

  return (
    <>
      <div className="actions">
        {can("server:start", { serverId: server.id }) &&
          actionApplies("start", state, desired) && (
            <button
              type="button"
              className="btn success"
              disabled={pending}
              onClick={() => lifecycle.mutate({ action: "start" })}
            >
              {t(
                state === "crashed"
                  ? "serverDetail.startCrashed"
                  : "serverDetail.start",
              )}
            </button>
          )}
        {can("server:stop", { serverId: server.id }) &&
          actionApplies("stop", state, desired) && (
            <StopControl
              disabled={pending}
              onStop={(force) =>
                lifecycle.mutate({
                  action: "stop",
                  query: force ? "force=true" : undefined,
                })
              }
            />
          )}
        {can("server:restart", { serverId: server.id }) &&
          actionApplies("restart", state, desired) && (
            <button
              type="button"
              className="btn"
              disabled={pending}
              onClick={() => lifecycle.mutate({ action: "restart" })}
            >
              {t("serverDetail.restart")}
            </button>
          )}
        {can("file:read", { serverId: server.id }) && (
          <button
            type="button"
            className="btn"
            disabled={pending || !atRest(state, desired)}
            onClick={() => exportMutation.mutate()}
          >
            {t("serverDetail.export")}
          </button>
        )}
      </div>
      <Modal
        open={eulaOpen}
        title={t("serverDetail.eulaDialog.title")}
        onClose={() => setEulaOpen(false)}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              onClick={() => setEulaOpen(false)}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn primary"
              onClick={() => {
                setEulaOpen(false);
                lifecycle.mutate({
                  action: "start",
                  query: "accept_eula=true",
                });
              }}
            >
              {t("serverDetail.eulaDialog.accept")}
            </button>
          </>
        }
      >
        <p>
          {t("serverDetail.eulaDialog.body")}{" "}
          <a
            href="https://aka.ms/MinecraftEULA"
            target="_blank"
            rel="noopener noreferrer"
          >
            {t("serverDetail.eulaDialog.link")}
          </a>
        </p>
      </Modal>
    </>
  );
}

// Use the menu-button keyboard pattern: roving focus, type-ahead, and Escape focus restoration.
function StopControl({
  disabled,
  onStop,
}: {
  disabled: boolean;
  onStop: (force: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirmForce, setConfirmForce] = useState(false);
  const [active, setActive] = useState(0);
  const ref = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const menu = [
    { labelKey: "serverDetail.stopGraceful", force: false, extra: "" },
    { labelKey: "serverDetail.stopForce", force: true, extra: " danger" },
  ] as const;

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };
  const activate = (force: boolean) => {
    setOpen(false);
    if (force) {
      setConfirmForce(true);
    } else {
      triggerRef.current?.focus();
      onStop(false);
    }
  };

  // Move DOM focus after the open state and active menu index commit.
  useLayoutEffect(() => {
    if (open) {
      itemRefs.current[active]?.focus();
    }
  }, [open, active]);

  // Document-level Escape handles the interval before focus enters the menu and restores the trigger.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onClick = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("click", onClick);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const onTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (
      event.key === "Enter" ||
      event.key === " " ||
      event.key === "ArrowDown"
    ) {
      event.preventDefault();
      setActive(0);
      setOpen(true);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive(menu.length - 1);
      setOpen(true);
    }
  };

  const onItemKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    i: number,
  ) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActive((i + 1) % menu.length);
        break;
      case "ArrowUp":
        event.preventDefault();
        setActive((i - 1 + menu.length) % menu.length);
        break;
      case "Home":
        event.preventDefault();
        setActive(0);
        break;
      case "End":
        event.preventDefault();
        setActive(menu.length - 1);
        break;
      case "Escape":
        event.preventDefault();
        close();
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        activate(menu[i].force);
        break;
      default: {
        if (event.key.length !== 1) {
          return;
        }
        const key = event.key.toLowerCase();
        const match = menu.findIndex((m) =>
          t(m.labelKey).toLowerCase().startsWith(key),
        );
        if (match !== -1) {
          event.preventDefault();
          setActive(match);
        }
      }
    }
  };

  return (
    <span className="stop-control" ref={ref}>
      <button
        ref={triggerRef}
        type="button"
        className="btn"
        disabled={disabled}
        onClick={() => {
          setActive(0);
          setOpen((v) => !v);
        }}
        onKeyDown={onTriggerKeyDown}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {t("serverDetail.stop")} ▾
      </button>
      {open && (
        <span className="stop-menu" role="menu">
          {menu.map((item, i) => (
            <button
              key={item.labelKey}
              ref={(el) => {
                itemRefs.current[i] = el;
              }}
              type="button"
              role="menuitem"
              tabIndex={active === i ? 0 : -1}
              className={`btn sm${item.extra}`}
              disabled={disabled}
              onClick={() => activate(item.force)}
              onKeyDown={(event) => onItemKeyDown(event, i)}
            >
              {t(item.labelKey)}
            </button>
          ))}
        </span>
      )}
      <Modal
        open={confirmForce}
        title={t("serverDetail.forceStop.dialogTitle")}
        onClose={() => {
          setConfirmForce(false);
          triggerRef.current?.focus();
        }}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              onClick={() => {
                setConfirmForce(false);
                triggerRef.current?.focus();
              }}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn danger"
              onClick={() => {
                setConfirmForce(false);
                triggerRef.current?.focus();
                onStop(true);
              }}
            >
              {t("serverDetail.forceStop.confirm")}
            </button>
          </>
        }
      >
        <p>{t("serverDetail.forceStop.dialogBody")}</p>
      </Modal>
    </span>
  );
}

function Overview({
  server,
  events,
  onOpenConsole,
}: {
  server: ServerResponse;
  events: ServerEventsState;
  onOpenConsole: () => void;
}) {
  // Exclude local RCON echoes from the overview tail; they belong only in the console.
  const logs = useLogs(events.logStore);
  const tail = logs
    .filter((e) => e.kind === "line" || e.kind === "gap")
    .slice(-TAIL_LINES);
  return (
    <section>
      <MetricsStrip
        samples={events.metrics}
        running={!atRest(normalizeState(server.observed_state))}
      />
      <div className="card log-tail">
        <div className="log-tail-head">
          <span className="log-tail-title">
            {t("serverDetail.logTailHeading")}
          </span>
          <button type="button" className="btn sm" onClick={onOpenConsole}>
            {t("serverDetail.openConsole")}
          </button>
        </div>
        {tail.length === 0 ? (
          <p className="sub">{t("serverDetail.logTailEmpty")}</p>
        ) : (
          <LogView entries={tail} follow={true} />
        )}
      </div>
    </section>
  );
}

function MetricsStrip({
  samples,
  running,
}: {
  samples: MetricsSample[];
  running: boolean;
}) {
  const latest = samples.at(-1);
  // Before metrics arrive, distinguish a live stream collecting samples from a stopped server with no stream.
  if (latest === undefined) {
    return (
      <div className="card metrics-strip metrics-strip-empty">
        <p>
          {t(
            running
              ? "serverDetail.metric.collecting"
              : "serverDetail.metric.idle",
          )}
        </p>
      </div>
    );
  }
  return (
    <div className="card metrics-strip">
      <Sparkline
        labelKey="serverDetail.metric.cpu"
        values={samples.map((s) => s.cpuMillis)}
        current={latest ? `${(latest.cpuMillis / 1000).toFixed(1)}` : null}
        unitKey="serverDetail.metric.cores"
      />
      <Sparkline
        labelKey="serverDetail.metric.memory"
        values={samples.map((s) => s.memoryBytes)}
        current={latest ? formatMiB(latest.memoryBytes) : null}
        unitKey="serverDetail.metric.mib"
      />
      <Sparkline
        labelKey="serverDetail.metric.players"
        values={samples.map((s) => s.playerCount)}
        current={latest ? String(latest.playerCount) : null}
        unitKey={null}
      />
    </div>
  );
}

function formatMiB(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(0);
}

// Scale the sample window to its own min/max so small variations remain visible.
function Sparkline({
  labelKey,
  values,
  current,
  unitKey,
}: {
  labelKey: TranslationKey;
  values: number[];
  current: string | null;
  unitKey: TranslationKey | null;
}) {
  const width = 120;
  const height = 28;
  return (
    <div className="metric">
      <div className="metric-label">{t(labelKey)}</div>
      <div className="metric-value">
        {current ?? "—"}
        {current !== null && unitKey !== null && (
          <span className="metric-unit"> {t(unitKey)}</span>
        )}
      </div>
      <svg
        className="sparkline"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={t(labelKey)}
      >
        {values.length >= 2 && (
          <polyline
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            points={sparklinePoints(values, width, height)}
          />
        )}
      </svg>
    </div>
  );
}

export function sparklinePoints(
  values: number[],
  width: number,
  height: number,
): string {
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const step = width / (values.length - 1);
  return values
    .map((v, i) => {
      const x = i * step;
      // Invert y because SVG's origin is at the top-left.
      const y = height - ((v - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

// Stable entry objects let memoized log rows skip rerendering when another entry arrives.
const LogRow = memo(function LogRow({ entry }: { entry: LogEntry }) {
  if (entry.kind === "gap") {
    return <div className="log-gap">{t("serverDetail.missedEvents")}</div>;
  }
  return (
    <div className={`log-line ${entry.kind}`}>
      {entry.kind === "command" ? "> " : ""}
      {entry.stripped}
    </div>
  );
});

function LogView({
  entries,
  follow,
}: {
  entries: LogEntry[];
  follow: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `entries` drives the auto-scroll; re-run on every append.
  useLayoutEffect(() => {
    if (follow && ref.current !== null) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [follow, entries]);
  return (
    <div className="log-view" ref={ref}>
      {entries.map((entry) => (
        <LogRow key={entry.id} entry={entry} />
      ))}
    </div>
  );
}

function Console({
  server,
  communityId,
  can,
  events,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
  events: ServerEventsState;
}) {
  const onForbidden = useOnForbidden();
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  // Track the clear boundary by stable entry ID so buffer trimming cannot shift it; -1 hides nothing.
  const [clearedThrough, setClearedThrough] = useState(-1);
  const [command, setCommand] = useState("");
  const history = useRef<string[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);

  const running = normalizeState(server.observed_state) === "running";
  const canCommand = can("server:command", { serverId: server.id });

  const send = useMutation({
    mutationFn: (line: string) =>
      api.post(
        apiPath("/api/communities/{community_id}/servers/{server_id}/command", {
          community_id: communityId,
          server_id: server.id,
        }),
        { body: JSON.stringify({ line }) },
      ),
    onSuccess: (data, line) => {
      const output = (data as { output: string }).output;
      events.appendLocal([
        { kind: "command", line },
        ...(output.length > 0
          ? [{ kind: "output" as const, line: stripMinecraftCodes(output) }]
          : []),
      ]);
    },
    onError: (error, line) => {
      if (onForbidden(error)) {
        return;
      }
      events.appendLocal([
        { kind: "command", line },
        { kind: "output", line: t(consoleCommandErrorPresentation(error)) },
      ]);
    },
  });

  const submit = () => {
    const line = command.trim();
    if (line.length === 0) {
      return;
    }
    history.current = [...history.current, line];
    setHistoryIndex(-1);
    setCommand("");
    send.mutate(line);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    const items = history.current;
    if (event.key === "ArrowUp" && items.length > 0) {
      event.preventDefault();
      const next =
        historyIndex === -1 ? items.length - 1 : Math.max(0, historyIndex - 1);
      setHistoryIndex(next);
      setCommand(items[next]);
    } else if (event.key === "ArrowDown" && historyIndex !== -1) {
      event.preventDefault();
      const next = historyIndex + 1;
      if (next >= items.length) {
        setHistoryIndex(-1);
        setCommand("");
      } else {
        setHistoryIndex(next);
        setCommand(items[next]);
      }
    } else if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  };

  const logs = useLogs(events.logStore);
  const needle = filter.trim().toLowerCase();
  const visible = logs.filter((entry) => {
    if (entry.id <= clearedThrough) {
      return false;
    }
    if (needle.length === 0 || entry.kind === "gap") {
      return true;
    }
    return entry.stripped.toLowerCase().includes(needle);
  });

  return (
    <section className="console">
      <div className="console-toolbar">
        <label className="console-follow">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
          />
          {t("serverDetail.console.follow")}
        </label>
        <input
          type="text"
          className="console-filter"
          placeholder={t("serverDetail.console.filter")}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <button
          type="button"
          className="btn sm"
          onClick={() => setClearedThrough(logs.at(-1)?.id ?? clearedThrough)}
        >
          {t("serverDetail.console.clear")}
        </button>
      </div>
      <div className="card console-stream">
        {visible.length === 0 ? (
          <p className="sub">{t("serverDetail.logTailEmpty")}</p>
        ) : (
          <LogView entries={visible} follow={follow} />
        )}
      </div>
      {canCommand && (
        <div className="console-input">
          <input
            type="text"
            value={command}
            disabled={!running || send.isPending}
            placeholder={
              running
                ? t("serverDetail.console.commandPlaceholder")
                : t("serverDetail.console.notRunning")
            }
            onChange={(e) => setCommand(e.target.value)}
            onKeyDown={onKeyDown}
          />
          <button
            type="button"
            className="btn primary"
            disabled={!running || send.isPending || command.trim().length === 0}
            onClick={submit}
          >
            {t("serverDetail.console.send")}
          </button>
        </div>
      )}
      {canCommand && !running && (
        <p className="field-hint">{t("serverDetail.console.notRunning")}</p>
      )}
    </section>
  );
}

// Hide platform-managed config keys but preserve them on save because PATCH replaces the entire config.
const SYSTEM_MANAGED_CONFIG_KEYS = new Set([
  "resolved_jar_sha256",
  "resolved_jar_source",
]);

// Memory has a dedicated field; exclude it from raw overrides and merge it on save.
const MEMORY_LIMIT_KEY = "memory_limit_mb";
// Match the API's lowercase DNS-label rule, including its 63-character limit.
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

// Blank omits slug from PATCH so the current address remains unchanged.
function slugValid(value: string): boolean {
  return value.trim() === "" || SLUG_RE.test(value.trim());
}

const MEMORY_LIMIT_FLOOR_MIB = 512;
const MEMORY_LIMIT_DEFAULT_CEILING_MIB = 1024 * 1024;

// CPU allocation is a relative share in millicores, not a hard cap; edit it through its dedicated field.
const CPU_ALLOCATION_KEY = "cpu_millis";
const CPU_ALLOCATION_FLOOR_MILLIS = 100;
const CPU_ALLOCATION_CEILING_MILLIS = 128_000;

const HIDDEN_CONFIG_KEYS = new Set([
  ...SYSTEM_MANAGED_CONFIG_KEYS,
  MEMORY_LIMIT_KEY,
  CPU_ALLOCATION_KEY,
]);

// Blank omits the memory limit; nonblank values must be integers within the operator-configured range.
function memoryLimitValid(
  value: string,
  ceiling: number = MEMORY_LIMIT_DEFAULT_CEILING_MIB,
): boolean {
  if (value.trim() === "") {
    return true;
  }
  const parsed = Number(value);
  return (
    Number.isInteger(parsed) &&
    parsed >= MEMORY_LIMIT_FLOOR_MIB &&
    parsed <= ceiling
  );
}

function cpuAllocationValid(value: string): boolean {
  if (value.trim() === "") {
    return true;
  }
  const parsed = Number(value);
  return (
    Number.isInteger(parsed) &&
    parsed >= CPU_ALLOCATION_FLOOR_MILLIS &&
    parsed <= CPU_ALLOCATION_CEILING_MILLIS
  );
}

interface ConfigRow {
  key: string;
  value: string;
  // Retain the original JSON type of untouched values instead of reparsing their display strings.
  original?: unknown;
  // Value edits require reparsing; a key-only rename preserves the original value type.
  edited?: boolean;
}

function toRows(config: Record<string, unknown>): ConfigRow[] {
  return Object.entries(config)
    .filter(([key]) => !HIDDEN_CONFIG_KEYS.has(key))
    .map(([key, value]) => ({
      key,
      value: typeof value === "string" ? value : JSON.stringify(value),
      original: value,
    }));
}

// Carry hidden managed keys through the full config replacement unchanged.
function systemManagedConfig(
  config: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(config).filter(([key]) =>
      SYSTEM_MANAGED_CONFIG_KEYS.has(key),
    ),
  );
}

// Parse valid JSON literals as typed values; keep other input as a string.
function parseConfigValue(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function fromRows(rows: ConfigRow[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const row of rows) {
    const trimmed = row.key.trim();
    if (trimmed.length === 0) {
      continue;
    }
    out[trimmed] =
      row.edited !== true && "original" in row
        ? row.original
        : parseConfigValue(row.value);
  }
  return out;
}

function Settings({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [name, setName] = useState(server.name);
  const [slug, setSlug] = useState(server.slug);
  const [slugError, setSlugError] = useState<string | null>(null);
  const [port, setPort] = useState(
    server.game_port !== null ? String(server.game_port) : "",
  );
  const [rows, setRows] = useState<ConfigRow[]>(() =>
    toRows(server.config as Record<string, unknown>),
  );
  const [memoryLimit, setMemoryLimit] = useState(
    typeof server.memory_limit_mb === "number"
      ? String(server.memory_limit_mb)
      : "",
  );
  const [cpuAllocation, setCpuAllocation] = useState(
    typeof server.cpu_millis === "number" ? String(server.cpu_millis) : "",
  );
  const [portHint, setPortHint] = useState<TranslationKey | null>(null);
  // Ignore earlier port checks that finish after a newer check.
  const portCheckRequest = useRef(0);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Resync after external updates or saves so stale form values do not overwrite refreshed server data.
  useEffect(() => {
    setName(server.name);
    setSlug(server.slug);
    setSlugError(null);
    portCheckRequest.current += 1;
    setPortHint(null);
    setPort(server.game_port !== null ? String(server.game_port) : "");
    setRows(toRows(server.config as Record<string, unknown>));
    setMemoryLimit(
      typeof server.memory_limit_mb === "number"
        ? String(server.memory_limit_mb)
        : "",
    );
    setCpuAllocation(
      typeof server.cpu_millis === "number" ? String(server.cpu_millis) : "",
    );
  }, [
    server.name,
    server.slug,
    server.game_port,
    server.config,
    server.memory_limit_mb,
    server.cpu_millis,
  ]);

  // Use /meta's operator-configured memory ceiling; share its query cache with server creation.
  const metaQuery = useQuery({
    queryKey: ["meta"],
    queryFn: ({ signal }) => api.get("/api/meta", { signal }),
  });
  const maxMemoryLimitMb: number =
    typeof metaQuery.data?.max_memory_limit_mb === "number"
      ? metaQuery.data.max_memory_limit_mb
      : MEMORY_LIMIT_DEFAULT_CEILING_MIB;

  // A join_hostname signals relay mode, where the API manages the hidden game port.
  const relayEnabled = server.join_hostname !== null;
  const canUpdate = can("server:update", { serverId: server.id });
  const memoryLimitOk = memoryLimitValid(memoryLimit, maxMemoryLimitMb);
  const cpuAllocationOk = cpuAllocationValid(cpuAllocation);
  const slugOk = slugValid(slug);
  const canDelete = can("server:delete", { serverId: server.id });
  const canExport = can("file:read", { serverId: server.id });

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    const { target, key } = serverSettingsErrorPresentation(
      error instanceof ApiError ? error.reason : undefined,
    );
    if (target === "slug") {
      setSlugError(t(key));
      return;
    }
    showToast(t(key), "error");
  };

  const save = useMutation({
    mutationFn: () =>
      api.patch(
        apiPath("/api/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: server.id,
        }),
        {
          body: JSON.stringify({
            name,
            ...(relayEnabled
              ? {}
              : { game_port: port === "" ? null : Number(port) }),
            ...(slug.trim() !== "" && slug.trim() !== server.slug
              ? { slug: slug.trim() }
              : {}),
            // Preserve hidden managed keys; omit cleared resource limits so the driver uses defaults.
            config: {
              ...systemManagedConfig(server.config as Record<string, unknown>),
              ...fromRows(rows),
              ...(memoryLimit.trim() === ""
                ? {}
                : { [MEMORY_LIMIT_KEY]: Number(memoryLimit) }),
              ...(cpuAllocation.trim() === ""
                ? {}
                : { [CPU_ALLOCATION_KEY]: Number(cpuAllocation) }),
            },
          }),
        },
      ),
    onSuccess: () => {
      setSlugError(null);
      showToast(t("serverDetail.settings.saved"), "success");
      queryClient.invalidateQueries({
        queryKey: serverKey(communityId, server.id),
      });
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
    },
    onError,
  });

  const remove = useMutation({
    mutationFn: () =>
      api.delete(
        apiPath("/api/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: server.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("serverDetail.deleted"), "success");
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
      navigate(dashboardPath(communityId));
    },
    onError: (error) => {
      setConfirmOpen(false);
      onError(error);
    },
  });

  const exportMutation = useMutation({
    mutationFn: exportViaGrant(communityId, server),
    onSuccess: () => showToast(t("serverDetail.exportStarted"), "success"),
    onError,
  });

  const checkPort = async () => {
    if (port === "" || Number(port) === server.game_port) {
      portCheckRequest.current += 1;
      setPortHint(
        Number(port) === server.game_port ? "serverDetail.port.current" : null,
      );
      return;
    }
    const parsed = Number(port);
    if (!Number.isInteger(parsed)) {
      portCheckRequest.current += 1;
      setPortHint("serverDetail.port.outOfRange");
      return;
    }
    const requestId = ++portCheckRequest.current;
    try {
      const result = await api.get(
        apiPath("/api/ports/check/{port}", { port: String(parsed) }),
      );
      if (portCheckRequest.current !== requestId) return;
      if (result.in_range === false) {
        setPortHint("serverDetail.port.outOfRange");
      } else if (result.available === false) {
        setPortHint("serverDetail.port.taken");
      } else {
        setPortHint("serverDetail.port.available");
      }
    } catch {
      if (portCheckRequest.current !== requestId) return;
      setPortHint("serverDetail.port.checkError");
    }
  };

  return (
    <section className="settings">
      <div className="card form-card">
        <h2>{t("serverDetail.settings.general")}</h2>
        <label className="field">
          {t("serverDetail.settings.name")}
          <input
            type="text"
            value={name}
            disabled={!canUpdate}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        {server.join_hostname !== null && (
          <label className="field">
            {t("serverDetail.settings.slug")}
            <input
              type="text"
              aria-label={t("serverDetail.settings.slug")}
              placeholder={server.slug}
              value={slug}
              disabled={!canUpdate}
              onChange={(e) => {
                setSlug(e.target.value);
                setSlugError(null);
              }}
            />
            {slugError !== null ? (
              <span className="field-error">{slugError}</span>
            ) : !slugOk ? (
              <span className="field-error">
                {t("serverDetail.settings.slugInvalid")}
              </span>
            ) : (
              <span className="field-hint">
                {t("serverDetail.settings.slugHint")}
              </span>
            )}
          </label>
        )}
        <div className="form-row">
          {relayEnabled ? null : (
            <label className="field">
              {t("serverDetail.settings.gamePort")}
              <input
                type="number"
                value={port}
                disabled={!canUpdate}
                onChange={(e) => setPort(e.target.value)}
                onBlur={() => void checkPort()}
              />
              {portHint !== null && (
                <span
                  className={
                    portHint === "serverDetail.port.available" ||
                    portHint === "serverDetail.port.current"
                      ? "field-hint ok"
                      : "field-error"
                  }
                >
                  {t(portHint)}
                </span>
              )}
            </label>
          )}
        </div>
        <label className="field">
          {t("serverDetail.settings.memoryLimit")}
          <input
            type="number"
            aria-label={t("serverDetail.settings.memoryLimit")}
            value={memoryLimit}
            disabled={!canUpdate}
            placeholder={t("serverDetail.settings.memoryLimitDefault")}
            onChange={(e) => setMemoryLimit(e.target.value)}
          />
          {memoryLimitOk ? (
            <span className="field-hint">
              {t("serverDetail.settings.memoryLimitHint")}
            </span>
          ) : (
            <span className="field-error">
              {t("serverDetail.settings.memoryLimitRange")}
            </span>
          )}
        </label>
        <label className="field">
          {t("serverDetail.settings.cpuAllocation")}
          <input
            type="number"
            aria-label={t("serverDetail.settings.cpuAllocation")}
            value={cpuAllocation}
            disabled={!canUpdate}
            placeholder={t("serverDetail.settings.cpuAllocationDefault")}
            onChange={(e) => setCpuAllocation(e.target.value)}
          />
          {cpuAllocationOk ? (
            <span className="field-hint">
              {t("serverDetail.settings.cpuAllocationHint")}
            </span>
          ) : (
            <span className="field-error">
              {t("serverDetail.settings.cpuAllocationRange")}
            </span>
          )}
        </label>
        <ConfigEditor rows={rows} disabled={!canUpdate} onChange={setRows} />
        <p className="field-hint">{t("serverDetail.settings.atRestHint")}</p>
        <button
          type="button"
          className="btn primary"
          disabled={
            !canUpdate ||
            !memoryLimitOk ||
            !cpuAllocationOk ||
            !slugOk ||
            save.isPending
          }
          onClick={() => save.mutate()}
        >
          {t("serverDetail.settings.save")}
        </button>
      </div>

      <ServerResourcePackSection
        server={server}
        communityId={communityId}
        can={can}
      />

      <div className="card danger-zone">
        <h2>{t("serverDetail.danger.heading")}</h2>
        {canExport && (
          <div className="row">
            <div>
              <strong>{t("serverDetail.danger.exportTitle")}</strong>
              <div className="desc">{t("serverDetail.danger.exportDesc")}</div>
            </div>
            <button
              type="button"
              className="btn"
              disabled={
                exportMutation.isPending ||
                !atRest(
                  normalizeState(server.observed_state),
                  normalizeState(server.desired_state),
                )
              }
              onClick={() => exportMutation.mutate()}
            >
              {t("serverDetail.danger.exportButton")}
            </button>
          </div>
        )}
        {canDelete && (
          <div className="row">
            <div>
              <strong>{t("serverDetail.danger.deleteTitle")}</strong>
              <div className="desc">{t("serverDetail.danger.deleteDesc")}</div>
            </div>
            <button
              type="button"
              className="btn danger"
              onClick={() => setConfirmOpen(true)}
            >
              {t("serverDetail.danger.deleteButton")}
            </button>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmOpen}
        title={t("serverDetail.delete.dialogTitle")}
        body={t("serverDetail.delete.dialogBody")}
        confirmPhrase={server.name}
        confirmLabel={t("serverDetail.delete.confirm")}
        promptLabel={t("serverDetail.delete.prompt")}
        onConfirm={() => remove.mutate()}
        onClose={() => setConfirmOpen(false)}
      />
    </section>
  );
}

function ConfigEditor({
  rows,
  disabled,
  onChange,
}: {
  rows: ConfigRow[];
  disabled: boolean;
  onChange: (rows: ConfigRow[]) => void;
}) {
  // A key-only rename must preserve a stored string such as "12" rather than coercing it to a number.
  const update = (i: number, patch: Partial<ConfigRow>) =>
    onChange(
      rows.map((r, j) =>
        j === i
          ? { ...r, ...patch, edited: r.edited === true || "value" in patch }
          : r,
      ),
    );
  return (
    <div className="field config-editor">
      <span>{t("serverDetail.settings.config")}</span>
      <span className="field-hint">
        {t("serverDetail.settings.configHint")}
      </span>
      {rows.map((row, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: positional override rows freely reordered/removed; a key-derived id would collide on duplicate/blank keys.
        <div className="config-row" key={i}>
          <input
            type="text"
            aria-label={t("serverDetail.settings.configKey")}
            placeholder={t("serverDetail.settings.configKey")}
            value={row.key}
            disabled={disabled}
            onChange={(e) => update(i, { key: e.target.value })}
          />
          <input
            type="text"
            aria-label={t("serverDetail.settings.configValue")}
            placeholder={t("serverDetail.settings.configValue")}
            value={row.value}
            disabled={disabled}
            onChange={(e) => update(i, { value: e.target.value })}
          />
          <button
            type="button"
            className="btn sm ghost"
            disabled={disabled}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            {t("serverDetail.settings.configRemove")}
          </button>
        </div>
      ))}
      <button
        type="button"
        className="btn sm"
        disabled={disabled}
        onClick={() => onChange([...rows, { key: "", value: "" }])}
      >
        {t("serverDetail.settings.configAdd")}
      </button>
    </div>
  );
}
