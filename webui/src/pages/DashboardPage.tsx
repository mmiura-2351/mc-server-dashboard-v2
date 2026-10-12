import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { Popover } from "../components/Popover.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { shortId } from "../format.ts";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { classifyQueryResult } from "../queryState.ts";
import { dashboardPath } from "../routes.ts";
import {
  isEulaNotAccepted,
  type LifecycleAction,
  lifecycleErrorMessage,
} from "./lifecycleErrors.ts";
import { stampedQueryFn } from "./restReads.ts";
import { ServerAddressBadges } from "./ServerAddressBadges.tsx";
import {
  actionApplies,
  bucketOf,
  normalizeState,
  type ObservedState,
  STATE_BUCKETS,
  type StateBucket,
  statePill,
  statesForBucket,
} from "./serverState.ts";
import { useFilterParams } from "./urlState.ts";
import { serversKey, useCommunityEvents } from "./useCommunityEvents.ts";
import {
  transitionalState,
  useLifecycleMutation,
} from "./useLifecycleMutation.ts";

type ServerResponse = components["schemas"]["ServerResponse"];

type ViewMode = "cards" | "table";
const VIEW_MODE_KEY = "mcsd.dashboard.viewMode";

// Persist view preference best-effort; unavailable or corrupt storage falls back to cards.
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
    // Ignore storage quota and availability failures.
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

type SortField = "name" | "state" | "type";
type SortDir = "asc" | "desc";
interface SortPref {
  field: SortField;
  dir: SortDir;
}
const SORT_KEY = "mcsd.dashboard.sort";
const DEFAULT_SORT: SortPref = { field: "name", dir: "asc" };

function loadSort(): SortPref {
  try {
    const raw = localStorage.getItem(SORT_KEY);
    if (raw === null) return DEFAULT_SORT;
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const field = parsed.field;
    const dir = parsed.dir;
    if (
      (field === "name" || field === "state" || field === "type") &&
      (dir === "asc" || dir === "desc")
    ) {
      return { field, dir };
    }
  } catch {
    // Unavailable or corrupt storage falls back to the default sort.
  }
  return DEFAULT_SORT;
}

function saveSort(pref: SortPref): void {
  try {
    localStorage.setItem(SORT_KEY, JSON.stringify(pref));
  } catch {
    // Sort persistence is best-effort when storage is unavailable or full.
  }
}

function useSortPref(): [SortPref, (next: SortPref) => void] {
  const [pref, setPref] = useState<SortPref>(() => loadSort());
  const update = (next: SortPref) => {
    setPref(next);
    saveSort(next);
  };
  return [pref, update];
}

function toggleSort(current: SortPref, field: SortField): SortPref {
  if (current.field === field) {
    return { field, dir: current.dir === "asc" ? "desc" : "asc" };
  }
  return { field, dir: "asc" };
}

const FILTER_KEYS = ["search", "state"] as const;

// Accept current bucket keys only and preserve canonical order; ignore stale raw-state bookmarks.
function parseStateBuckets(stateFilter: string): StateBucket[] {
  const tokens = stateFilter ? stateFilter.split(",").filter(Boolean) : [];
  return STATE_BUCKETS.filter((bucket) => tokens.includes(bucket));
}

// Filter by observed-state buckets; no selected buckets means all servers.
function filterServers(
  servers: ServerResponse[],
  search: string,
  stateFilter: string,
): ServerResponse[] {
  const needle = search.trim().toLowerCase();
  const buckets = new Set(parseStateBuckets(stateFilter));
  return servers.filter((s) => {
    if (needle && !s.name.toLowerCase().includes(needle)) return false;
    if (
      buckets.size > 0 &&
      !buckets.has(bucketOf(normalizeState(s.observed_state)))
    )
      return false;
    return true;
  });
}

function sortServers(
  servers: ServerResponse[],
  pref: SortPref,
): ServerResponse[] {
  const sorted = [...servers];
  sorted.sort((a, b) => {
    let cmp: number;
    switch (pref.field) {
      case "name":
        cmp = a.name.localeCompare(b.name);
        break;
      case "state":
        cmp = normalizeState(a.observed_state).localeCompare(
          normalizeState(b.observed_state),
        );
        break;
      case "type":
        cmp = a.server_type.localeCompare(b.server_type);
        break;
    }
    // Break primary-sort ties by case-insensitive name for stable ordering.
    if (cmp === 0 && pref.field !== "name") {
      cmp = a.name.localeCompare(b.name);
    }
    return pref.dir === "desc" ? -cmp : cmp;
  });
  return sorted;
}

export function DashboardPage() {
  // Use the URL community ID; the active community may differ on stale bookmarks.
  // Use memberships only to check access to that URL target.
  const { cid } = useParams();
  const { communities } = useActiveCommunity();

  if (communities === undefined) {
    return (
      <DashboardChrome>
        <p className="sub">{t("dashboard.loading")}</p>
      </DashboardChrome>
    );
  }
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
  const [sort, setSort] = useSortPref();
  const [filters, setFilters] = useFilterParams(FILTER_KEYS);

  // Filter immediately with a local draft; write the URL on blur or Enter to avoid a history entry per keystroke.
  const [searchDraft, setSearchDraft] = useState(filters.search);
  useEffect(() => {
    setSearchDraft(filters.search);
  }, [filters.search]);

  const commitSearch = useCallback(() => {
    if (searchDraft !== filters.search) {
      setFilters({ ...filters, search: searchDraft });
    }
  }, [searchDraft, filters, setFilters]);

  const query = useQuery({
    queryKey: serversKey(communityId),
    // Stamp REST attempts so live frames can be ordered against them.
    queryFn: stampedQueryFn(({ signal }) =>
      api.get(
        apiPath("/api/communities/{community_id}/servers", {
          community_id: communityId,
        }),
        { signal },
      ),
    ),
  });
  const resultState = classifyQueryResult(query);

  const servers = resultState.kind === "data" ? resultState.data : [];
  const filtered = useMemo(
    () => filterServers(servers, searchDraft, filters.state),
    [servers, searchDraft, filters.state],
  );
  const sorted = useMemo(() => sortServers(filtered, sort), [filtered, sort]);

  if (resultState.kind === "pending") {
    return (
      <DashboardChrome>
        <p className="sub">{t("dashboard.loading")}</p>
      </DashboardChrome>
    );
  }
  // Retain cached content when only a background refetch fails; stream status is reported separately.
  if (resultState.kind === "error") {
    return (
      <DashboardChrome>
        <p className="field-error">{t("dashboard.loadError")}</p>
      </DashboardChrome>
    );
  }

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
      <DashboardFilterBar
        filters={filters}
        searchDraft={searchDraft}
        onSearchDraftChange={setSearchDraft}
        onSearchCommit={commitSearch}
        onFiltersChange={setFilters}
        sort={sort}
        onSortChange={setSort}
        view={view}
      />
      {sorted.length === 0 ? (
        <div className="empty">
          <p className="sub">{t("dashboard.filter.noMatch")}</p>
        </div>
      ) : view === "table" ? (
        <ServerTable
          servers={sorted}
          communityId={communityId}
          can={can}
          sort={sort}
          onSort={setSort}
        />
      ) : (
        <div className="grid cols-2">
          {sorted.map((server) => (
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

function DashboardFilterBar({
  filters,
  searchDraft,
  onSearchDraftChange,
  onSearchCommit,
  onFiltersChange,
  sort,
  onSortChange,
  view,
}: {
  filters: Record<"search" | "state", string>;
  searchDraft: string;
  onSearchDraftChange: (value: string) => void;
  onSearchCommit: () => void;
  onFiltersChange: (next: Record<"search" | "state", string>) => void;
  sort: SortPref;
  onSortChange: (next: SortPref) => void;
  view: ViewMode;
}) {
  const activeBuckets = useMemo(
    () => new Set<StateBucket>(parseStateBuckets(filters.state)),
    [filters.state],
  );

  const toggleBucket = (bucket: StateBucket) => {
    const next = new Set(activeBuckets);
    if (next.has(bucket)) {
      next.delete(bucket);
    } else {
      next.add(bucket);
    }
    // Serialize bucket keys in canonical order to keep equivalent URLs stable.
    onFiltersChange({
      ...filters,
      state: STATE_BUCKETS.filter((b) => next.has(b)).join(","),
    });
  };

  const clearBuckets = () => onFiltersChange({ ...filters, state: "" });

  return (
    <div className="dashboard-filters">
      <input
        type="text"
        className="filter-search"
        placeholder={t("dashboard.filter.search")}
        value={searchDraft}
        onChange={(e) => onSearchDraftChange(e.target.value)}
        onBlur={onSearchCommit}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSearchCommit();
        }}
        aria-label={t("dashboard.filter.search")}
      />
      <Popover
        buttonAriaLabel={
          activeBuckets.size > 0
            ? t("dashboard.filter.stateCount", { count: activeBuckets.size })
            : t("dashboard.filter.state")
        }
        buttonClassName="btn sm filter-state-trigger"
        panelClassName="filter-state-menu"
        label={
          <>
            {t("dashboard.filter.stateLabel")}
            <span aria-hidden="true"> ▾</span>
            {activeBuckets.size > 0 && (
              <span className="badge count" aria-hidden="true">
                {activeBuckets.size}
              </span>
            )}
          </>
        }
      >
        <fieldset
          className="filter-state-buckets"
          aria-label={t("dashboard.filter.state")}
        >
          {STATE_BUCKETS.map((bucket) => {
            // Reuse a representative state's color; statesForBucket owns the bucket-to-state mapping.
            const pill = statePill(statesForBucket(bucket)[0]);
            return (
              <label key={bucket} className="filter-state-option">
                <input
                  type="checkbox"
                  checked={activeBuckets.has(bucket)}
                  onChange={() => toggleBucket(bucket)}
                />
                <span className={`pill ${pill.className}`}>
                  {t(`dashboard.filter.bucket.${bucket}`)}
                </span>
              </label>
            );
          })}
          <button
            type="button"
            className="btn sm ghost filter-state-clear"
            disabled={activeBuckets.size === 0}
            onClick={clearBuckets}
          >
            {t("dashboard.filter.clear")}
          </button>
        </fieldset>
      </Popover>
      {view === "cards" && (
        <SortControl sort={sort} onSortChange={onSortChange} />
      )}
    </div>
  );
}

const SORT_FIELDS: {
  field: SortField;
  labelKey:
    | "dashboard.sort.name"
    | "dashboard.sort.state"
    | "dashboard.sort.type";
}[] = [
  { field: "name", labelKey: "dashboard.sort.name" },
  { field: "state", labelKey: "dashboard.sort.state" },
  { field: "type", labelKey: "dashboard.sort.type" },
];

function SortControl({
  sort,
  onSortChange,
}: {
  sort: SortPref;
  onSortChange: (next: SortPref) => void;
}) {
  return (
    <fieldset className="sort-control" aria-label={t("dashboard.sort.label")}>
      <span className="sort-label">{t("dashboard.sort.label")}:</span>
      {SORT_FIELDS.map(({ field, labelKey }) => (
        <button
          key={field}
          type="button"
          className="btn sm"
          aria-pressed={sort.field === field}
          onClick={() => onSortChange(toggleSort(sort, field))}
        >
          {t(labelKey)}
          {sort.field === field && (sort.dir === "asc" ? " ▲" : " ▼")}
        </button>
      ))}
    </fieldset>
  );
}

function SortableHeader({
  field,
  sort,
  onSort,
  children,
}: {
  field: SortField;
  sort: SortPref;
  onSort: (next: SortPref) => void;
  children: ReactNode;
}) {
  const active = sort.field === field;
  const handleSort = () => onSort(toggleSort(sort, field));
  return (
    <th
      className="sortable"
      aria-sort={
        active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"
      }
      tabIndex={0}
      onClick={handleSort}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          handleSort();
        }
      }}
    >
      {children}
      {active && (
        <span aria-hidden="true">{sort.dir === "asc" ? " ▲" : " ▼"}</span>
      )}
    </th>
  );
}

function useLifecycle(server: ServerResponse, communityId: string) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [eulaOpen, setEulaOpen] = useState(false);

  const mutation = useLifecycleMutation({
    communityId,
    serverId: server.id,
    cacheKey: serversKey(communityId),
    invalidateKeys: [serversKey(communityId)],
    onError: (error, action) => {
      // Handle 403 through capability refresh; pass other failures and the requested verb to the shared mapper.
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

  const acceptEulaAndStart = useCallback(async () => {
    setEulaOpen(false);
    const path = apiPath(
      "/api/communities/{community_id}/servers/{server_id}/start",
      { community_id: communityId, server_id: server.id },
    );
    try {
      await api.post(`${path}?accept_eula=true` as never);
      queryClient.invalidateQueries({ queryKey: serversKey(communityId) });
    } catch (error: unknown) {
      if (onForbidden(error)) {
        return;
      }
      showToast(t(lifecycleErrorMessage(error, "start")), "error");
    }
  }, [communityId, server.id, queryClient, onForbidden, showToast]);

  const state = normalizeState(server.observed_state);
  // Show the requested transition immediately while the lifecycle request is pending.
  const displayState: ObservedState = mutation.isPending
    ? transitionalState(mutation.variables.action)
    : state;

  return {
    mutation,
    state,
    displayState,
    eulaOpen,
    setEulaOpen,
    acceptEulaAndStart,
  };
}

function StatePill({ state }: { state: ObservedState }) {
  const pill = statePill(state);
  return (
    <span className={`pill ${pill.className}${pill.blink ? " blink" : ""}`}>
      {t(pill.labelKey)}
    </span>
  );
}

// Show desired/observed drift beside the pill using the detail page's rule.
// Filtering still follows observed state, including rows with pending intent.
function DriftPill({ server }: { server: ServerResponse }) {
  if (server.desired_state === server.observed_state) {
    return null;
  }
  return (
    <span className="pill settling blink" role="status">
      {t("serverDetail.converging")}
    </span>
  );
}

interface ServerRowProps {
  server: ServerResponse;
  communityId: string;
  can: Can;
}

function ServerCard({ server, communityId, can }: ServerRowProps) {
  const {
    mutation,
    state,
    displayState,
    eulaOpen,
    setEulaOpen,
    acceptEulaAndStart,
  } = useLifecycle(server, communityId);

  return (
    <div className="card server-card">
      <div className="head">
        <span className="name">
          <Link to={`${dashboardPath(communityId)}/servers/${server.id}`}>
            {server.name}
          </Link>
        </span>
        <StatePill state={displayState} />
        <DriftPill server={server} />
      </div>
      <div className="meta">
        <span className="badge type">
          {server.server_type} {server.mc_version}
        </span>
        <ServerAddressBadges
          server={server}
          buttonClassName="badge copyable"
          fallback={
            server.game_port !== null && (
              <span className="badge">:{server.game_port}</span>
            )
          }
        />
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
            onRun={(action) => mutation.mutate({ action })}
          />
        ))}
        <span className="right" title={server.assigned_worker_id ?? undefined}>
          {server.assigned_worker_id !== null
            ? `${t("dashboard.col.worker")}: ${shortId(server.assigned_worker_id)}`
            : t("dashboard.noWorker")}
        </span>
      </div>
      <EulaModal
        open={eulaOpen}
        onClose={() => setEulaOpen(false)}
        onAccept={acceptEulaAndStart}
      />
    </div>
  );
}

function ServerTable({
  servers,
  communityId,
  can,
  sort,
  onSort,
}: {
  servers: ServerResponse[];
  communityId: string;
  can: Can;
  sort: SortPref;
  onSort: (next: SortPref) => void;
}) {
  return (
    <div className="card dashboard-table" style={{ padding: 0 }}>
      <ResizableTable storageKey="mcsd.colw.dashboard-servers" className="data">
        <thead>
          <tr>
            <SortableHeader field="name" sort={sort} onSort={onSort}>
              {t("dashboard.col.name")}
            </SortableHeader>
            <SortableHeader field="state" sort={sort} onSort={onSort}>
              {t("dashboard.col.state")}
            </SortableHeader>
            <SortableHeader field="type" sort={sort} onSort={onSort}>
              {t("dashboard.col.type")}
            </SortableHeader>
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
  const {
    mutation,
    state,
    displayState,
    eulaOpen,
    setEulaOpen,
    acceptEulaAndStart,
  } = useLifecycle(server, communityId);

  return (
    <tr>
      <td>
        <Link to={`${dashboardPath(communityId)}/servers/${server.id}`}>
          {server.name}
        </Link>
      </td>
      <td>
        <StatePill state={displayState} />
        <DriftPill server={server} />
      </td>
      <td>
        {server.server_type} {server.mc_version}
      </td>
      <td>
        <ServerAddressBadges
          server={server}
          buttonClassName="copyable"
          buttonStyle={{
            cursor: "pointer",
            background: "none",
            border: "none",
            padding: 0,
            font: "inherit",
            color: "inherit",
          }}
          fallback={server.game_port ?? "—"}
        />
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
            onRun={(action) => mutation.mutate({ action })}
          />
        ))}
        <EulaModal
          open={eulaOpen}
          onClose={() => setEulaOpen(false)}
          onAccept={acceptEulaAndStart}
        />
      </td>
    </tr>
  );
}

function EulaModal({
  open,
  onClose,
  onAccept,
}: {
  open: boolean;
  onClose: () => void;
  onAccept: () => void;
}) {
  return (
    <Modal
      open={open}
      title={t("serverDetail.eulaDialog.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button type="button" className="btn primary" onClick={onAccept}>
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

function Action({ action, server, state, pending, can, onRun }: ActionProps) {
  if (!can(`server:${action}`, { serverId: server.id })) {
    return null;
  }
  const disabled =
    pending ||
    !actionApplies(action, state, normalizeState(server.desired_state));
  return (
    <button
      type="button"
      className={`btn sm${action === "start" ? " success" : ""}`}
      disabled={disabled}
      onClick={() => onRun(action)}
    >
      {t(
        action === "start" && state === "crashed"
          ? "dashboard.startCrashed"
          : LABEL_KEY[action],
      )}
    </button>
  );
}

const LABEL_KEY = {
  start: "dashboard.start",
  stop: "dashboard.stop",
  restart: "dashboard.restart",
} as const;
