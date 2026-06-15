import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type KeyboardEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { shortId } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { dashboardPath } from "../routes.ts";
import { isEulaNotAccepted, lifecycleErrorMessage } from "./lifecycleErrors.ts";
import { ServerBackupsTab } from "./ServerBackupsTab.tsx";
import { ServerFilesTab } from "./ServerFilesTab.tsx";
import { ServerPlayersTab } from "./ServerPlayersTab.tsx";
import { serverKey } from "./serverKey.ts";
import {
  actionApplies,
  atRest,
  normalizeState,
  statePill,
} from "./serverState.ts";
import { useTabHash } from "./urlState.ts";
import { serversKey } from "./useCommunityEvents.ts";
import {
  type LogEntry,
  type MetricsSample,
  type ServerEventsState,
  TAIL_LINES,
  useServerEvents,
} from "./useServerEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

export { serverKey };

const TABS = [
  "overview",
  "console",
  "files",
  "backups",
  "players",
  "settings",
] as const;
type Tab = (typeof TABS)[number];

const TAB_LABEL: Record<Tab, TranslationKey> = {
  overview: "serverDetail.tab.overview",
  console: "serverDetail.tab.console",
  files: "serverDetail.tab.files",
  backups: "serverDetail.tab.backups",
  players: "serverDetail.tab.players",
  settings: "serverDetail.tab.settings",
};

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
  // Active tab lives in the URL hash (#514) so Back walks the tab history;
  // #overview is the default and keeps a clean URL.
  const [tab, setTab] = useTabHash(TABS);
  // One WS per open detail page, shared by all tabs (WEBUI_SPEC.md 7.2).
  const events = useServerEvents(communityId, serverId);
  const query = useQuery({
    queryKey: serverKey(communityId, serverId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: serverId,
        }),
      ),
  });

  if (query.isPending) {
    return <p className="sub">{t("serverDetail.loading")}</p>;
  }
  if (query.isError || query.data === undefined) {
    return <p className="field-error">{t("serverDetail.loadError")}</p>;
  }

  const server = query.data;
  return (
    <>
      <Header
        server={server}
        communityId={communityId}
        can={can}
        degraded={events.degraded}
        statusDetail={events.statusDetail}
      />
      <div className="tabs" role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={`tab${tab === name ? " active" : ""}`}
            onClick={() => setTab(name)}
          >
            {t(TAB_LABEL[name])}
          </button>
        ))}
      </div>
      {tab === "overview" && (
        <Overview
          server={server}
          events={events}
          onOpenConsole={() => setTab("console")}
        />
      )}
      {tab === "console" && (
        <Console
          server={server}
          communityId={communityId}
          can={can}
          events={events}
        />
      )}
      {tab === "files" && (
        <ServerFilesTab server={server} communityId={communityId} can={can} />
      )}
      {tab === "backups" && (
        <ServerBackupsTab server={server} communityId={communityId} can={can} />
      )}
      {tab === "players" && (
        <ServerPlayersTab
          communityId={communityId}
          serverId={server.id}
          can={can}
        />
      )}
      {tab === "settings" && (
        <Settings server={server} communityId={communityId} can={can} />
      )}
    </>
  );
}

// ── Overview header + lifecycle controls ────────────────────────────────────

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
      ta.style.opacity = "0";
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

function Header({
  server,
  communityId,
  can,
  degraded,
  statusDetail,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
  degraded: boolean;
  statusDetail: string;
}) {
  const state = normalizeState(server.observed_state);
  const pill = statePill(state);
  const desired = normalizeState(server.desired_state);
  // The reconciler has not yet converged when desired ≠ observed; show a
  // settling hint (WEBUI_SPEC.md 6.4).
  const drifting = server.desired_state !== server.observed_state;

  // Clickable-copy state for the join-hostname badge.
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
        {(state === "crashed" || state === "unknown") &&
          statusDetail.length > 0 && (
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
          <span className="badge">{server.execution_backend}</span>
          {server.join_hostname !== null ? (
            <button
              type="button"
              className="badge copyable"
              title={server.join_hostname}
              onClick={handleCopy}
            >
              {copied
                ? t("serverDetail.copiedJoinHostname")
                : server.join_hostname}
            </button>
          ) : (
            <span className="badge">
              {server.game_port !== null
                ? `:${server.game_port}`
                : t("serverDetail.noPort")}
            </span>
          )}
          <span
            className="badge"
            title={server.assigned_worker_id ?? undefined}
          >
            {server.assigned_worker_id !== null
              ? `${t("serverDetail.worker")}: ${shortId(server.assigned_worker_id)}`
              : t("serverDetail.noWorker")}
          </span>
          {" · "}
          {t("serverDetail.desired")}: {desired}
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
  const queryClient = useQueryClient();
  const state = normalizeState(server.observed_state);
  const desired = normalizeState(server.desired_state);
  const [eulaOpen, setEulaOpen] = useState(false);

  const invalidate = () => {
    queryClient.invalidateQueries({
      queryKey: serverKey(communityId, server.id),
    });
    queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
  };
  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t(lifecycleErrorMessage(error)), "error");
  };

  const lifecycle = useMutation({
    mutationFn: (path: string) => api.post(path as never),
    onMutate: (path: string) => {
      // Optimistically set the observed_state to the transitional state so
      // the pill transitions instantly, before the API responds (#1071).
      const action = path.split("/").pop()?.replace(/\?.*/, "") ?? "";
      const transitional =
        action === "stop"
          ? "stopping"
          : action === "restart"
            ? "restarting"
            : "starting";
      const key = serverKey(communityId, server.id);
      const previous = queryClient.getQueryData<ServerResponse>(key);
      queryClient.setQueryData<ServerResponse>(key, (old) =>
        old ? { ...old, observed_state: transitional } : old,
      );
      return { previous };
    },
    onSettled: invalidate,
    onError: (error, _path, context) => {
      // Rollback the optimistic cache update before showing the error toast.
      if (context?.previous) {
        queryClient.setQueryData(
          serverKey(communityId, server.id),
          context.previous,
        );
      }
      if (onForbidden(error)) {
        return;
      }
      if (isEulaNotAccepted(error)) {
        setEulaOpen(true);
        return;
      }
      showToast(t(lifecycleErrorMessage(error)), "error");
    },
  });

  const exportMutation = useMutation({
    mutationFn: () =>
      downloadFile(
        apiPath("/api/communities/{community_id}/servers/{server_id}/export", {
          community_id: communityId,
          server_id: server.id,
        }),
        `${server.name}.zip`,
      ),
    onSuccess: () => showToast(t("serverDetail.exportStarted"), "success"),
    onError,
  });

  const base = `/api/communities/${communityId}/servers/${server.id}`;
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
              onClick={() => lifecycle.mutate(`${base}/start`)}
            >
              {t("serverDetail.start")}
            </button>
          )}
        {can("server:stop", { serverId: server.id }) &&
          actionApplies("stop", state, desired) && (
            <StopControl
              disabled={pending}
              onStop={(force) =>
                lifecycle.mutate(`${base}/stop${force ? "?force=true" : ""}`)
              }
            />
          )}
        {can("server:restart", { serverId: server.id }) &&
          actionApplies("restart", state, desired) && (
            <button
              type="button"
              className="btn"
              disabled={pending}
              onClick={() => lifecycle.mutate(`${base}/restart`)}
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
                lifecycle.mutate(`${base}/start?accept_eula=true`);
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

// Stop with a graceful/force choice (WEBUI_SPEC.md 6.4). The bare button stops
// gracefully; the caret toggles a small menu whose entries pick the mode.
//
// The menu follows the WAI-ARIA menu-button keyboard pattern (#496): a roving
// tabindex (only the active item is tabbable), arrow keys / Home / End move
// focus, type-ahead jumps to the next item by first letter, Enter/Space
// activates, and Escape closes and returns focus to the trigger. Two items only,
// so the logic is kept local rather than hoisted into a generic menu primitive.
function StopControl({
  disabled,
  onStop,
}: {
  disabled: boolean;
  onStop: (force: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  // When true the force-stop confirmation modal is shown.
  const [confirmForce, setConfirmForce] = useState(false);
  // Index of the focused menu item while open; drives the roving tabindex.
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

  // Move focus to the menu item once open/active settles, so opening with a key
  // lands focus on the right entry and arrow keys keep DOM focus in step.
  useLayoutEffect(() => {
    if (open) {
      itemRefs.current[active]?.focus();
    }
  }, [open, active]);

  // Close the menu on a click outside it or on Escape, mirroring the
  // document-listener pattern in Modal.tsx (listeners attached only while open).
  // Escape returns focus to the trigger so keyboard users are not stranded; the
  // per-item handler below also catches it (and stops propagation), so this is
  // the fallback for an Escape pressed before focus has entered the menu.
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

  // Open from the trigger via the keyboard: Enter/Space/Down focus the first
  // item, Up focuses the last (WAI-ARIA menu-button pattern).
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
        // Type-ahead: jump to the next item whose label starts with the key.
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
  // Last ~200 lines of the live stream; local RCON echoes are console-only, so
  // the tail shows only real server output / gap markers (WEBUI_SPEC.md 6.4).
  const tail = events.logs
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
      <dl className="kv card">
        <dt>{t("serverDetail.observed")}</dt>
        <dd>{server.observed_state}</dd>
        <dt>{t("serverDetail.desired")}</dt>
        <dd>{server.desired_state}</dd>
      </dl>
    </section>
  );
}

// ── Metrics strip (WEBUI_SPEC.md 6.4) ───────────────────────────────────────

function MetricsStrip({
  samples,
  running,
}: {
  samples: MetricsSample[];
  running: boolean;
}) {
  const latest = samples.at(-1);
  // Until the first frame arrives, say so honestly: "collecting" while the
  // server is live — running or still coming up (frames are sparse, ~10-15s) —
  // else "no metrics while stopped" since SPEC 7.2 has no metrics stream when
  // the server is at rest.
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
        // CPU in milli-cores → cores, one decimal.
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

// A self-drawn sparkline: an inline-SVG polyline over the sample window, scaled
// to its own min/max so the shape reads even for a flat-but-nonzero series. No
// charting dependency (WEBUI_SPEC.md 6.4: self-drawn, client-side only).
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

/** Map a value series to a polyline `points` string spanning the viewbox. */
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
      // Invert y: SVG origin is top-left, so a higher value sits higher up.
      const y = height - ((v - min) / span) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

// ── Shared log view + Console tab (WEBUI_SPEC.md 6.5) ────────────────────────

// The log stream renderer shared by the Overview tail and the Console. Each
// entry is color-keyed by source (stdout/stderr) or echo kind (command/output);
// a gap entry renders the inline "missed events" divider (WEBUI_SPEC.md 7.2).
// In follow mode it auto-scrolls to the newest line on each update.
function LogView({
  entries,
  follow,
}: {
  entries: LogEntry[];
  follow: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  // Re-scroll to the newest line whenever the entries change while following.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `entries` drives the auto-scroll; re-run on every append.
  useLayoutEffect(() => {
    if (follow && ref.current !== null) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [follow, entries]);
  return (
    <div className="log-view" ref={ref}>
      {entries.map((entry) =>
        entry.kind === "gap" ? (
          <div key={entry.id} className="log-gap">
            {t("serverDetail.missedEvents")}
          </div>
        ) : (
          <div key={entry.id} className={`log-line ${entry.kind}`}>
            {entry.kind === "command" ? "> " : ""}
            {entry.line}
          </div>
        ),
      )}
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
  // Clear hides everything up to (and including) this entry id; -1 hides none.
  // Keyed by stable id, not position, so the bounded-buffer trim cannot shift it.
  const [clearedThrough, setClearedThrough] = useState(-1);
  const [command, setCommand] = useState("");
  // Local command history for ↑/↓; -1 means "not browsing history".
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
          ? [{ kind: "output" as const, line: output }]
          : []),
      ]);
    },
    onError: (error, line) => {
      if (onForbidden(error)) {
        return;
      }
      events.appendLocal([
        { kind: "command", line },
        { kind: "output", line: t("serverDetail.commandFailed") },
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

  // ↑/↓ browse the local history; at the bottom (index -1) the input is blank.
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

  const needle = filter.trim().toLowerCase();
  const visible = events.logs.filter((entry) => {
    if (entry.id <= clearedThrough) {
      return false;
    }
    if (needle.length === 0 || entry.kind === "gap") {
      return true;
    }
    return entry.line.toLowerCase().includes(needle);
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
          onClick={() =>
            setClearedThrough(events.logs.at(-1)?.id ?? clearedThrough)
          }
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

// ── Settings tab ────────────────────────────────────────────────────────────

// Config keys the system manages/derives, not the user: they are hidden from the
// overrides editor (issue #645) so they can't be edited or deleted, but their
// values are preserved across a save (see `Settings`) since the PATCH replaces
// the whole `config` blob. `resolved_jar_sha256` is the resolved-JAR content
// address written by the start path (servers/domain/value_objects.py).
const SYSTEM_MANAGED_CONFIG_KEYS = new Set(["resolved_jar_sha256"]);

// The per-server memory limit (issue #709) rides the `config` blob as a reserved
// key (unit: MiB), but it has a dedicated field rather than a raw override row,
// so it is hidden from the overrides editor and merged back explicitly on save.
const MEMORY_LIMIT_KEY = "memory_limit_mb";
// Relay slug validation — mirror the API rule (RELAY.md Section 15):
// a lowercase DNS label: starts/ends with [a-z0-9], up to 63 chars total,
// internal chars may include hyphens.
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

// A slug input is valid when it matches the DNS-label regex; blank is also
// allowed (leave field empty → keep the current slug by omitting it from PATCH).
function slugValid(value: string): boolean {
  return value.trim() === "" || SLUG_RE.test(value.trim());
}

// Mirror the API validator (servers/domain/memory_limit.py): a whole number in
// [512, 1 TiB] MiB; the API rejects out-of-range values as `invalid_memory_limit`.
const MEMORY_LIMIT_FLOOR_MIB = 512;
const MEMORY_LIMIT_DEFAULT_CEILING_MIB = 1024 * 1024;

// The per-server CPU allocation (issue #726) rides the `config` blob the same
// way as the memory limit, but the value is a *soft, relative share* (millicores,
// 1000 = one core), not a hard cap. It has a dedicated field rather than a raw
// override row, so it is hidden from the overrides editor and merged back on save.
const CPU_ALLOCATION_KEY = "cpu_millis";
// Mirror the API validator (servers/domain/cpu_allocation.py): a whole number in
// [100, 128000] millicores; the API rejects out-of-range values as
// `invalid_cpu_allocation`.
const CPU_ALLOCATION_FLOOR_MILLIS = 100;
const CPU_ALLOCATION_CEILING_MILLIS = 128_000;

// The keys hidden from the overrides editor: system-managed plus the memory
// limit and CPU allocation, which each have their own field.
const HIDDEN_CONFIG_KEYS = new Set([
  ...SYSTEM_MANAGED_CONFIG_KEYS,
  MEMORY_LIMIT_KEY,
  CPU_ALLOCATION_KEY,
]);

// A non-blank memory-limit input is valid only as a whole number within range;
// blank means "unset → driver default" and is always allowed. The ceiling can
// be overridden by the operator-configurable max (#1069).
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

// A non-blank CPU-allocation input is valid only as a whole number within range;
// blank means "unset → auto" and is always allowed.
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
  // The original JSON value of this key as loaded from the server, preserved
  // so an untouched row round-trips its exact type (e.g. an integer
  // `snapshot_interval_seconds` stays a number) rather than being re-parsed
  // from its display string. `undefined` for rows the user added.
  original?: unknown;
  // Set once the user edits this row's value (or key), so a save reparses the
  // string instead of reusing `original`.
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

// The system-managed entries (issue #645) the editor hides but a save must carry
// back unchanged, since the PATCH replaces the whole `config` blob.
function systemManagedConfig(
  config: Record<string, unknown>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(config).filter(([key]) =>
      SYSTEM_MANAGED_CONFIG_KEYS.has(key),
    ),
  );
}

// Parse a value-input string with JSON-value semantics: a valid JSON literal
// (number / boolean / null / object / array) keeps that type, anything else
// (including a bare word like `hard`) stays a string. So `12` round-trips as a
// number and the API's non-bool-int cadence keys (snapshot_interval_seconds,
// backup_interval_hours) are not sent as strings → no spurious 422.
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
    // An untouched loaded row keeps its original typed value; an edited or new
    // row is parsed from its display string.
    out[trimmed] =
      row.edited !== true && "original" in row
        ? row.original
        : parseConfigValue(row.value);
  }
  return out;
}

// Map a settings save/delete error reason to a specific message; otherwise the
// generic toast. 422 carries a port reason (port_out_of_range) or a cadence
// reason (invalid_snapshot_interval / invalid_backup_schedule), 409 the at-rest
// gate (server_not_stopped) and export the unsettled gate.
function settingsErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    switch (error.reason) {
      case "server_not_stopped":
        return "serverDetail.error.notStopped";
      case "server_unsettled":
        return "serverDetail.error.unsettled";
      case "port_taken":
        return "serverDetail.error.portTaken";
      case "port_out_of_range":
        return "serverDetail.error.portOutOfRange";
      case "invalid_snapshot_interval":
        return "serverDetail.error.invalidSnapshotInterval";
      case "invalid_backup_schedule":
        return "serverDetail.error.invalidBackupSchedule";
      case "invalid_memory_limit":
        return "serverDetail.error.invalidMemoryLimit";
      case "invalid_cpu_allocation":
        return "serverDetail.error.invalidCpuAllocation";
      case "invalid_slug":
        return "serverDetail.error.invalidSlug";
      case "slug_taken":
        return "serverDetail.error.slugTaken";
    }
  }
  return "serverDetail.error.generic";
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
  // Empty string ↔ unset (driver default); a number ↔ the limit in MiB.
  const [memoryLimit, setMemoryLimit] = useState(
    typeof server.memory_limit_mb === "number"
      ? String(server.memory_limit_mb)
      : "",
  );
  // Empty string ↔ unset (auto); a number ↔ the allocation in millicores.
  const [cpuAllocation, setCpuAllocation] = useState(
    typeof server.cpu_millis === "number" ? String(server.cpu_millis) : "",
  );
  const [portHint, setPortHint] = useState<TranslationKey | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Operator-configurable memory-limit ceiling from /meta (issue #1069). The
  // meta query is shared with the create page via react-query's cache.
  const metaQuery = useQuery({
    queryKey: ["meta"],
    queryFn: () => api.get("/api/meta"),
  });
  const maxMemoryLimitMb: number =
    typeof metaQuery.data?.max_memory_limit_mb === "number"
      ? metaQuery.data.max_memory_limit_mb
      : MEMORY_LIMIT_DEFAULT_CEILING_MIB;

  // Relay mode is signalled by a non-null join_hostname (the API exposes it only
  // when relay.enabled). In relay mode the game port is hidden and API-managed
  // (#1002); in direct mode it stays editable.
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
    // Surface slug-specific errors inline on the field rather than as toasts.
    if (error instanceof ApiError) {
      if (error.reason === "invalid_slug" || error.reason === "slug_taken") {
        setSlugError(
          error.reason === "slug_taken"
            ? t("serverDetail.settings.slugTaken")
            : t("serverDetail.settings.slugInvalid"),
        );
        return;
      }
    }
    showToast(t(settingsErrorMessage(error)), "error");
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
            // In relay mode the port is hidden and API-managed, so omit it from
            // the PATCH; in direct mode send the edited value (#1002).
            ...(relayEnabled
              ? {}
              : { game_port: port === "" ? null : Number(port) }),
            // Include slug rename only when the field is non-empty and differs
            // from the current value; omit otherwise so the API keeps the slug.
            ...(slug.trim() !== "" && slug.trim() !== server.slug
              ? { slug: slug.trim() }
              : {}),
            // Re-merge the hidden system-managed keys (issue #645) so the full
            // config replace doesn't drop them, then layer the memory limit
            // (issue #709) and CPU allocation (issue #726): a number when set,
            // omitted when cleared so the server falls back to the driver
            // default / auto share.
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
    mutationFn: () =>
      downloadFile(
        apiPath("/api/communities/{community_id}/servers/{server_id}/export", {
          community_id: communityId,
          server_id: server.id,
        }),
        `${server.name}.zip`,
      ),
    onSuccess: () => showToast(t("serverDetail.exportStarted"), "success"),
    onError,
  });

  const checkPort = async () => {
    if (port === "" || Number(port) === server.game_port) {
      setPortHint(
        Number(port) === server.game_port ? "serverDetail.port.current" : null,
      );
      return;
    }
    const parsed = Number(port);
    if (!Number.isInteger(parsed)) {
      setPortHint("serverDetail.port.outOfRange");
      return;
    }
    try {
      const result = await api.get(
        apiPath("/api/ports/check/{port}", { port: String(parsed) }),
      );
      if (result.in_range === false) {
        setPortHint("serverDetail.port.outOfRange");
      } else if (result.available === false) {
        setPortHint("serverDetail.port.taken");
      } else {
        setPortHint("serverDetail.port.available");
      }
    } catch {
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
          {/* In relay mode players join via the slug hostname (port-less); the
              game port is internal plumbing the API auto-allocates, so hide the
              control. Direct mode still needs a port-forward, so keep it (#1002). */}
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
          <label className="field">
            {t("serverDetail.settings.executionBackend")}
            <input type="text" value={server.execution_backend} disabled />
            <span className="field-hint">
              {t("serverDetail.settings.executionBackendHint")}
            </span>
          </label>
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
  // Only mark a row edited when its VALUE changes; a key-only rename must not
  // trigger re-parsing of the display string, so a stored "12" (string) stays
  // a string and doesn't silently coerce to the number 12 on save (#791).
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
