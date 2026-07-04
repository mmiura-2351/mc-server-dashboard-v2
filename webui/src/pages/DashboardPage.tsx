import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { Link, useParams } from "react-router";
import { api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { copyToClipboard } from "../clipboard.ts";
import { Modal } from "../components/Modal.tsx";
import { ResizableTable } from "../components/ResizableColumns.tsx";
import { useToast } from "../components/Toast.tsx";
import { shortId } from "../format.ts";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "../permissions/ActiveCommunityProvider.tsx";
import { type Can, useCan } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { dashboardPath } from "../routes.ts";
import { isEulaNotAccepted, lifecycleErrorMessage } from "./lifecycleErrors.ts";
import {
  actionApplies,
  KNOWN,
  normalizeState,
  type ObservedState,
  statePill,
} from "./serverState.ts";
import { useFilterParams } from "./urlState.ts";
import { serversKey, useCommunityEvents } from "./useCommunityEvents.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type LifecycleAction = "start" | "stop" | "restart";

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

// Sort preference (#1123), persisted in localStorage like the view mode.
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
    // Corrupt/blocked storage.
  }
  return DEFAULT_SORT;
}

function saveSort(pref: SortPref): void {
  try {
    localStorage.setItem(SORT_KEY, JSON.stringify(pref));
  } catch {
    // Best-effort.
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

// Toggle sort: clicking the same field flips direction; a new field resets to asc.
function toggleSort(current: SortPref, field: SortField): SortPref {
  if (current.field === field) {
    return { field, dir: current.dir === "asc" ? "desc" : "asc" };
  }
  return { field, dir: "asc" };
}

// URL-driven filter keys for the dashboard (#1123).
const FILTER_KEYS = ["search", "state"] as const;

// Apply client-side filtering to the server list.
function filterServers(
  servers: ServerResponse[],
  search: string,
  stateFilter: string,
): ServerResponse[] {
  const needle = search.trim().toLowerCase();
  const states = stateFilter ? stateFilter.split(",").filter(Boolean) : [];
  return servers.filter((s) => {
    if (needle && !s.name.toLowerCase().includes(needle)) return false;
    if (states.length > 0 && !states.includes(normalizeState(s.observed_state)))
      return false;
    return true;
  });
}

// Apply client-side sorting to the server list.
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
    // Secondary sort by name (case-insensitive) for deterministic ordering
    // when the primary field values are equal.
    if (cmp === 0 && pref.field !== "name") {
      cmp = a.name.localeCompare(b.name);
    }
    return pref.dir === "desc" ? -cmp : cmp;
  });
  return sorted;
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
  const [sort, setSort] = useSortPref();
  const [filters, setFilters] = useFilterParams(FILTER_KEYS);

  // The search input uses a local draft so each keystroke does not push a
  // history entry. Filtering applies the draft immediately for instant
  // feedback; the URL is updated on blur / Enter.
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
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers", {
          community_id: communityId,
        }),
      ),
  });

  // Hooks must be called unconditionally (Rules of Hooks), so filter/sort are
  // computed before the early-return branches. They operate on an empty array
  // when the query has not resolved yet.
  const servers = query.data ?? [];
  const filtered = useMemo(
    () => filterServers(servers, searchDraft, filters.state),
    [servers, searchDraft, filters.state],
  );
  const sorted = useMemo(() => sortServers(filtered, sort), [filtered, sort]);

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

// Filter and sort controls rendered above both card and table views (#1123).
// The search input uses a local draft (owned by Loaded) to avoid pushing a
// browser history entry per keystroke; the draft syncs to the URL on blur or
// Enter (the audit pages use a separate "Apply" button for the same reason).
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
  const activeStates = useMemo(
    () =>
      new Set(filters.state ? filters.state.split(",").filter(Boolean) : []),
    [filters.state],
  );

  const toggleState = (state: string) => {
    const next = new Set(activeStates);
    if (next.has(state)) {
      next.delete(state);
    } else {
      next.add(state);
    }
    onFiltersChange({
      ...filters,
      state: [...next].sort().join(","),
    });
  };

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
      <fieldset
        className="filter-states"
        aria-label={t("dashboard.filter.state")}
      >
        {/* "unknown" is excluded: it is a fallback for unrecognised API values,
            not a state users intentionally filter for. */}
        {KNOWN.filter((s) => s !== "unknown").map((state) => {
          const pill = statePill(state);
          return (
            <button
              key={state}
              type="button"
              className={`pill ${pill.className}${activeStates.has(state) ? "" : " dim"}`}
              aria-pressed={activeStates.has(state)}
              onClick={() => toggleState(state)}
            >
              {t(pill.labelKey)}
            </button>
          );
        })}
      </fieldset>
      {/* In card view, show an explicit sort control; table view uses column headers. */}
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

// A sortable table column header that keeps the label text intact (for
// getByText-style test queries) and renders the direction indicator in a
// separate aria-hidden span.
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

// The per-server lifecycle mutation plus the observed/optimistic state, shared by
// the card and table rows so neither duplicates the start/stop/restart business
// logic (#541).
function useLifecycle(server: ServerResponse, communityId: string) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [eulaOpen, setEulaOpen] = useState(false);

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
      if (isEulaNotAccepted(error)) {
        setEulaOpen(true);
        return;
      }
      showToast(t(lifecycleErrorMessage(error)), "error");
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
      showToast(t(lifecycleErrorMessage(error)), "error");
    }
  }, [communityId, server.id, queryClient, onForbidden, showToast]);

  const state = normalizeState(server.observed_state);
  // While a request is in flight, show the transition it requests so the pill
  // moves immediately, before the list refetch returns.
  const displayState: ObservedState = mutation.isPending
    ? requestedState(mutation.variables)
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
  const {
    mutation,
    state,
    displayState,
    eulaOpen,
    setEulaOpen,
    acceptEulaAndStart,
  } = useLifecycle(server, communityId);
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Bedrock address:port badge (issue #1543): its own copy state, mirroring
  // the Java join-hostname badge above.
  const [bedrockCopied, setBedrockCopied] = useState(false);
  const bedrockCopyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
      if (bedrockCopyTimerRef.current !== null) {
        clearTimeout(bedrockCopyTimerRef.current);
      }
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

  const bedrockAddress =
    server.bedrock_address !== null && server.bedrock_port !== null
      ? `${server.bedrock_address}:${server.bedrock_port}`
      : null;

  const handleCopyBedrock = useCallback(() => {
    if (server.bedrock_address === null) return;
    if (bedrockCopyTimerRef.current !== null) {
      clearTimeout(bedrockCopyTimerRef.current);
    }
    // Copy the host only: Bedrock's "Add Server" screen has a separate Port
    // field, and pasting `host:port` into the address field fails validation.
    copyToClipboard(server.bedrock_address).then(
      () => {
        setBedrockCopied(true);
        bedrockCopyTimerRef.current = setTimeout(
          () => setBedrockCopied(false),
          1500,
        );
      },
      () => {
        setBedrockCopied(false);
      },
    );
  }, [server.bedrock_address]);

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
        {bedrockAddress !== null && (
          <button
            type="button"
            className="badge copyable"
            title={t("dashboard.bedrockAddressCopyTitle", {
              port: server.bedrock_port ?? "",
            })}
            onClick={handleCopyBedrock}
          >
            {bedrockCopied ? (
              t("dashboard.copiedBedrockAddress")
            ) : (
              <>
                {t("dashboard.bedrockLabel")}: {server.bedrock_address}:
                {server.bedrock_port}
              </>
            )}
          </button>
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
      <EulaModal
        open={eulaOpen}
        onClose={() => setEulaOpen(false)}
        onAccept={acceptEulaAndStart}
      />
    </div>
  );
}

// Compact table view (#541): the same servers/data as the cards, reusing the
// ResizableTable column affordances and the shared serverState helpers.
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
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Bedrock address:port badge (issue #1543): its own copy state, mirroring
  // the Java join-hostname button above.
  const [bedrockCopied, setBedrockCopied] = useState(false);
  const bedrockCopyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
      if (bedrockCopyTimerRef.current !== null) {
        clearTimeout(bedrockCopyTimerRef.current);
      }
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

  const bedrockAddress =
    server.bedrock_address !== null && server.bedrock_port !== null
      ? `${server.bedrock_address}:${server.bedrock_port}`
      : null;

  const handleCopyBedrock = useCallback(() => {
    if (server.bedrock_address === null) return;
    if (bedrockCopyTimerRef.current !== null) {
      clearTimeout(bedrockCopyTimerRef.current);
    }
    // Copy the host only: Bedrock's "Add Server" screen has a separate Port
    // field, and pasting `host:port` into the address field fails validation.
    copyToClipboard(server.bedrock_address).then(
      () => {
        setBedrockCopied(true);
        bedrockCopyTimerRef.current = setTimeout(
          () => setBedrockCopied(false),
          1500,
        );
      },
      () => {
        setBedrockCopied(false);
      },
    );
  }, [server.bedrock_address]);

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
        {bedrockAddress !== null && (
          <button
            type="button"
            className="copyable"
            title={t("dashboard.bedrockAddressCopyTitle", {
              port: server.bedrock_port ?? "",
            })}
            style={{
              cursor: "pointer",
              background: "none",
              border: "none",
              padding: 0,
              font: "inherit",
              color: "inherit",
            }}
            onClick={handleCopyBedrock}
          >
            {bedrockCopied ? (
              t("dashboard.copiedBedrockAddress")
            ) : (
              <>
                {t("dashboard.bedrockLabel")}: {server.bedrock_address}:
                {server.bedrock_port}
              </>
            )}
          </button>
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

// One lifecycle button. Rendered only when permitted (controls render from
// permissions, WEBUI_SPEC.md 7.3); disabled while a request is in flight or the
// action does not apply to the current state.
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
