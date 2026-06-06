import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { dashboardPath } from "../routes.ts";
import { lifecycleErrorMessage } from "./lifecycleErrors.ts";
import {
  actionApplies,
  atRest,
  normalizeState,
  statePill,
} from "./serverState.ts";
import { serversKey } from "./useCommunityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

/** Query key for a single server's detail (cid + sid scoped). */
export function serverKey(communityId: string, serverId: string) {
  return ["server", communityId, serverId] as const;
}

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
  const [tab, setTab] = useState<Tab>("overview");
  const query = useQuery({
    queryKey: serverKey(communityId, serverId),
    queryFn: () =>
      api.get(
        apiPath("/communities/{community_id}/servers/{server_id}", {
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
      <Header server={server} communityId={communityId} can={can} />
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
      {tab === "overview" && <Overview server={server} />}
      {tab === "settings" && (
        <Settings server={server} communityId={communityId} can={can} />
      )}
      {tab !== "overview" && tab !== "settings" && (
        <p className="sub">{t("serverDetail.tabPlaceholder")}</p>
      )}
    </>
  );
}

// ── Overview header + lifecycle controls ────────────────────────────────────

function Header({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const state = normalizeState(server.observed_state);
  const pill = statePill(state);
  const desired = normalizeState(server.desired_state);
  // The reconciler has not yet converged when desired ≠ observed; show a
  // settling hint (WEBUI_SPEC.md 6.4).
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
        <h1 className="detail-title">
          {server.name}
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
        </h1>
        <div className="sub">
          <span className="badge type">
            {server.server_type} {server.mc_version}
          </span>
          <span className="badge">{server.execution_backend}</span>
          <span className="badge">
            {server.game_port !== null
              ? `:${server.game_port}`
              : t("serverDetail.noPort")}
          </span>
          <span className="badge">
            {server.assigned_worker_id ?? t("serverDetail.noWorker")}
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
    onSettled: invalidate,
    onError,
  });

  const exportMutation = useMutation({
    mutationFn: () =>
      downloadFile(
        apiPath("/communities/{community_id}/servers/{server_id}/export", {
          community_id: communityId,
          server_id: server.id,
        }),
        `${server.name}.zip`,
      ),
    onSuccess: () => showToast(t("serverDetail.exportStarted"), "success"),
    onError,
  });

  const base = `/communities/${communityId}/servers/${server.id}`;
  const pending = lifecycle.isPending || exportMutation.isPending;

  return (
    <div className="actions">
      {can("server:start", { serverId: server.id }) &&
        actionApplies("start", state) && (
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
        actionApplies("stop", state) && (
          <StopControl
            disabled={pending}
            onStop={(force) =>
              lifecycle.mutate(`${base}/stop${force ? "?force=true" : ""}`)
            }
          />
        )}
      {can("server:restart", { serverId: server.id }) &&
        actionApplies("restart", state) && (
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
          disabled={pending || !atRest(state)}
          onClick={() => exportMutation.mutate()}
        >
          {t("serverDetail.export")}
        </button>
      )}
    </div>
  );
}

// Stop with a graceful/force choice (WEBUI_SPEC.md 6.4). The bare button stops
// gracefully; the caret toggles a small menu whose entries pick the mode.
function StopControl({
  disabled,
  onStop,
}: {
  disabled: boolean;
  onStop: (force: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  // Close the menu on a click outside it, mirroring the document-listener
  // pattern in Modal.tsx (listener attached only while open).
  useEffect(() => {
    if (!open) {
      return;
    }
    const onClick = (event: MouseEvent) => {
      if (!ref.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, [open]);
  return (
    <span className="stop-control" ref={ref}>
      <button
        type="button"
        className="btn"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {t("serverDetail.stop")} ▾
      </button>
      {open && (
        <span className="stop-menu" role="menu">
          <button
            type="button"
            role="menuitem"
            className="btn sm"
            disabled={disabled}
            onClick={() => {
              setOpen(false);
              onStop(false);
            }}
          >
            {t("serverDetail.stopGraceful")}
          </button>
          <button
            type="button"
            role="menuitem"
            className="btn sm danger"
            disabled={disabled}
            onClick={() => {
              setOpen(false);
              onStop(true);
            }}
          >
            {t("serverDetail.stopForce")}
          </button>
        </span>
      )}
    </span>
  );
}

function Overview({ server }: { server: ServerResponse }) {
  return (
    <section>
      {/* Live metrics + log tail arrive with the per-server WS stream (#440). */}
      <div className="card metrics-placeholder" aria-hidden="true">
        <p className="sub">{t("serverDetail.metricsPlaceholder")}</p>
      </div>
      <div className="card log-placeholder" aria-hidden="true">
        <p className="sub">{t("serverDetail.logTailPlaceholder")}</p>
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

// ── Settings tab ────────────────────────────────────────────────────────────

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
  return Object.entries(config).map(([key, value]) => ({
    key,
    value: typeof value === "string" ? value : JSON.stringify(value),
    original: value,
  }));
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
  const [port, setPort] = useState(
    server.game_port !== null ? String(server.game_port) : "",
  );
  const [rows, setRows] = useState<ConfigRow[]>(() =>
    toRows(server.config as Record<string, unknown>),
  );
  const [portHint, setPortHint] = useState<TranslationKey | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const canUpdate = can("server:update", { serverId: server.id });
  const canDelete = can("server:delete", { serverId: server.id });
  const canExport = can("file:read", { serverId: server.id });

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t(settingsErrorMessage(error)), "error");
  };

  const save = useMutation({
    mutationFn: () =>
      api.patch(
        apiPath("/communities/{community_id}/servers/{server_id}", {
          community_id: communityId,
          server_id: server.id,
        }),
        {
          body: JSON.stringify({
            name,
            game_port: port === "" ? null : Number(port),
            config: fromRows(rows),
          }),
        },
      ),
    onSuccess: () => {
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
        apiPath("/communities/{community_id}/servers/{server_id}", {
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
        apiPath("/communities/{community_id}/servers/{server_id}/export", {
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
      const result = (await api.get(
        apiPath("/ports/check/{port}", { port: String(parsed) }),
      )) as { in_range?: boolean; available?: boolean };
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
        <div className="form-row">
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
          <label className="field">
            {t("serverDetail.settings.executionBackend")}
            <input type="text" value={server.execution_backend} disabled />
            <span className="field-hint">
              {t("serverDetail.settings.executionBackendHint")}
            </span>
          </label>
        </div>
        <ConfigEditor rows={rows} disabled={!canUpdate} onChange={setRows} />
        <p className="field-hint">{t("serverDetail.settings.atRestHint")}</p>
        <button
          type="button"
          className="btn primary"
          disabled={!canUpdate || save.isPending}
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
                !atRest(normalizeState(server.observed_state))
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
  const update = (i: number, patch: Partial<ConfigRow>) =>
    onChange(
      rows.map((r, j) => (j === i ? { ...r, ...patch, edited: true } : r)),
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
