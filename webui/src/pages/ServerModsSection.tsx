/**
 * Server detail — Mod management section (issue #1267).
 *
 * A card rendered in the Settings tab showing the server's assigned mod set:
 * each mod's name/version/loader, a side badge, and an enabled/disabled state.
 * The user can multi-select-assign from the library, unassign, and toggle a
 * mod enabled/disabled. A dependency/compatibility checklist renders the
 * validation findings from the same `GET .../mods` response, and a button
 * bulk-downloads the client modpack zip.
 *
 * Mutating actions are gated on server:update and server-at-rest (the API
 * otherwise answers 409 server_unsettled); reads (list, validation, download)
 * are always allowed. Mirrors ServerResourcePackSection.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { type TranslationKey, t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type ServerModListResponse = components["schemas"]["ServerModListResponse"];
type ServerModResponse = components["schemas"]["ServerModResponse"];
type ModValidationResponse = components["schemas"]["ModValidationResponse"];
type ModResponse = components["schemas"]["ModResponse"];

// The library `side` axis (issue #1258), rendered as a localized badge.
// Anything outside the known set falls back to the raw value.
const SIDE_LABEL: Record<string, TranslationKey> = {
  server: "mods.side.server",
  client: "mods.side.client",
  both: "mods.side.both",
};

// Which library mod loaders a server loader can run (issue #1286). Mirrors the
// canonical `_LOADER_COMPAT` map in
// `api/.../servers/application/mod_validation.py`; the assign-dialog filter and
// the backend validation must agree on which loaders are compatible. Keep the
// two obviously in sync. A server loader absent from this map matches nothing.
const LOADER_COMPAT: Record<string, readonly string[]> = {
  fabric: ["fabric", "quilt"],
  forge: ["forge", "neoforge"],
  paper: ["paper"],
  spigot: ["paper"],
  vanilla: [],
};

function SideBadge({ side }: { side: string }) {
  const key = SIDE_LABEL[side];
  return <span className="badge">{key !== undefined ? t(key) : side}</span>;
}

const EMPTY_VALIDATION: ModValidationResponse = {
  missing_deps: [],
  version_unsatisfied: [],
  conflicts: [],
  loader_mismatch: [],
  mc_mismatch: [],
};

function modsKey(communityId: string, serverId: string) {
  return ["server-mods", communityId, serverId] as const;
}

function modsErrorMessage(
  error: unknown,
  fallback: TranslationKey,
): TranslationKey {
  if (error instanceof ApiError) {
    if (error.reason === "server_unsettled")
      return "serverDetail.error.unsettled";
    if (error.reason === "server_not_stopped")
      return "serverDetail.error.notStopped";
  }
  return fallback;
}

export function ServerModsSection({
  server,
  communityId,
  can,
}: {
  server: ServerResponse;
  communityId: string;
  can: Can;
}) {
  const serverId = server.id;
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const [assignOpen, setAssignOpen] = useState(false);

  const canUpdate = can("server:update", { serverId });
  const serverAtRest = atRest(
    normalizeState(server.observed_state),
    normalizeState(server.desired_state),
  );

  const listQuery = useQuery({
    queryKey: modsKey(communityId, serverId),
    queryFn: async (): Promise<ServerModListResponse> => {
      const result = await api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}/mods", {
          community_id: communityId,
          server_id: serverId,
        }),
      );
      // Guard the shape (mirrors ServerResourcePackSection): a response without
      // the expected `mods` array degrades to an empty set rather than crashing.
      if (
        result === undefined ||
        typeof result !== "object" ||
        !Array.isArray((result as { mods?: unknown }).mods)
      ) {
        return { mods: [], validation: EMPTY_VALIDATION };
      }
      return result as ServerModListResponse;
    },
  });

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: modsKey(communityId, serverId),
    });
  };

  const unassign = useMutation({
    mutationFn: (modId: string) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/mods/{mod_id}",
          { community_id: communityId, server_id: serverId, mod_id: modId },
        ),
      ),
    onSuccess: () => {
      showToast(t("serverMods.unassigned"), "success");
      refresh();
    },
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(
        t(modsErrorMessage(error, "serverMods.unassignError")),
        "error",
      );
    },
  });

  const toggle = useMutation({
    mutationFn: ({ modId, enabled }: { modId: string; enabled: boolean }) =>
      api.post(
        enabled
          ? apiPath(
              "/api/communities/{community_id}/servers/{server_id}/mods/{mod_id}/disable",
              { community_id: communityId, server_id: serverId, mod_id: modId },
            )
          : apiPath(
              "/api/communities/{community_id}/servers/{server_id}/mods/{mod_id}/enable",
              { community_id: communityId, server_id: serverId, mod_id: modId },
            ),
      ),
    onSuccess: () => {
      refresh();
    },
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t(modsErrorMessage(error, "serverMods.toggleError")), "error");
    },
  });

  const download = useMutation({
    mutationFn: () =>
      downloadFile(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/client-mods/download",
          { community_id: communityId, server_id: serverId },
        ),
        `${server.slug}-client-mods.zip`,
      ),
    onError: (error) => {
      if (onForbidden(error)) return;
      showToast(t("serverMods.downloadError"), "error");
    },
  });

  if (listQuery.isPending) {
    return null;
  }

  if (listQuery.isError) {
    return (
      <div className="card form-card">
        <h2>{t("serverMods.heading")}</h2>
        <p className="field-error" role="alert">
          {t("serverMods.loadError")}
        </p>
      </div>
    );
  }

  const mods = listQuery.data.mods;
  const validation = listQuery.data.validation;
  // Resolve a finding's mod_id to a display name where possible (the finding
  // references the assigned mod the user can act on).
  const nameOf = (modId: string): string =>
    mods.find((m) => m.mod.id === modId)?.mod.display_name ?? modId;

  return (
    <div className="card form-card">
      <h2>{t("serverMods.heading")}</h2>

      {mods.length === 0 ? (
        <p className="sub">{t("serverMods.none")}</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>{t("serverMods.col.name")}</th>
              <th>{t("serverMods.col.version")}</th>
              <th>{t("serverMods.col.loader")}</th>
              <th>{t("serverMods.col.side")}</th>
              <th>{t("serverMods.col.state")}</th>
              {canUpdate && <th />}
            </tr>
          </thead>
          <tbody>
            {mods.map((entry) => (
              <ModRow
                key={entry.mod.id}
                entry={entry}
                canUpdate={canUpdate}
                serverAtRest={serverAtRest}
                onToggle={() =>
                  toggle.mutate({
                    modId: entry.mod.id,
                    enabled: entry.enabled,
                  })
                }
                onUnassign={() => unassign.mutate(entry.mod.id)}
                busy={toggle.isPending || unassign.isPending}
              />
            ))}
          </tbody>
        </table>
      )}

      <ValidationChecklist validation={validation} nameOf={nameOf} />

      <div className="actions">
        {canUpdate && (
          <button
            type="button"
            className="btn primary"
            disabled={!serverAtRest}
            onClick={() => setAssignOpen(true)}
          >
            {t("serverMods.assign")}
          </button>
        )}
        <button
          type="button"
          className="btn"
          disabled={download.isPending}
          onClick={() => download.mutate()}
        >
          {t("serverMods.downloadClient")}
        </button>
      </div>
      {canUpdate && !serverAtRest && (
        <p className="field-hint">{t("serverMods.notAtRest")}</p>
      )}

      {assignOpen && (
        <AssignDialog
          communityId={communityId}
          serverId={serverId}
          serverLoader={server.server_type}
          assignedIds={new Set(mods.map((m) => m.mod.id))}
          onSuccess={() => {
            setAssignOpen(false);
            showToast(t("serverMods.assigned"), "success");
            refresh();
          }}
          onClose={() => setAssignOpen(false)}
        />
      )}
    </div>
  );
}

function ModRow({
  entry,
  canUpdate,
  serverAtRest,
  onToggle,
  onUnassign,
  busy,
}: {
  entry: ServerModResponse;
  canUpdate: boolean;
  serverAtRest: boolean;
  onToggle: () => void;
  onUnassign: () => void;
  busy: boolean;
}) {
  const mod = entry.mod;
  return (
    <tr>
      <td>{mod.display_name}</td>
      <td>{mod.version_number}</td>
      <td>{mod.loader_type}</td>
      <td>
        <SideBadge side={mod.side} />
      </td>
      <td>{t(entry.enabled ? "serverMods.enabled" : "serverMods.disabled")}</td>
      {canUpdate && (
        <td className="row-actions">
          <button
            type="button"
            className="btn sm"
            disabled={!serverAtRest || busy}
            onClick={onToggle}
          >
            {t(entry.enabled ? "serverMods.disable" : "serverMods.enable")}
          </button>
          <button
            type="button"
            className="btn sm danger"
            disabled={!serverAtRest || busy}
            onClick={onUnassign}
          >
            {t("serverMods.unassign")}
          </button>
        </td>
      )}
    </tr>
  );
}

function ValidationChecklist({
  validation,
  nameOf,
}: {
  validation: ModValidationResponse;
  nameOf: (modId: string) => string;
}) {
  const total =
    validation.missing_deps.length +
    validation.version_unsatisfied.length +
    validation.conflicts.length +
    validation.loader_mismatch.length +
    validation.mc_mismatch.length;

  return (
    <div className="server-mods-validation">
      <h3>{t("serverMods.validation.heading")}</h3>
      {total === 0 ? (
        <p className="field-hint">{t("serverMods.validation.ok")}</p>
      ) : (
        <ul>
          {validation.missing_deps.map((finding) => (
            <li
              key={`dep-${finding.mod_id}-${finding.depends_on}`}
              className="field-error"
            >
              {t("serverMods.validation.missingDep")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{dependency}", finding.depends_on)
                .replace("{range}", finding.version_range)}
            </li>
          ))}
          {validation.version_unsatisfied.map((finding) => (
            <li
              key={`version-${finding.mod_id}-${finding.depends_on}`}
              className="field-error"
            >
              {t("serverMods.validation.versionUnsatisfied")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{dependency}", finding.depends_on)
                .replace("{range}", finding.version_range)
                .replace("{present}", finding.present_version)}
            </li>
          ))}
          {validation.conflicts.map((finding) => (
            <li
              key={`conflict-${finding.mod_id}-${finding.conflicts_with}`}
              className="field-error"
            >
              {t("serverMods.validation.conflict")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{other}", finding.conflicts_with)}
            </li>
          ))}
          {validation.loader_mismatch.map((finding) => (
            <li key={`loader-${finding.mod_id}`} className="field-error">
              {t("serverMods.validation.loaderMismatch")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{modLoader}", finding.mod_loader)
                .replace("{serverLoader}", finding.server_loader)}
            </li>
          ))}
          {validation.mc_mismatch.map((finding) => (
            <li key={`mc-${finding.mod_id}`} className="field-hint warn">
              {t("serverMods.validation.mcMismatch")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{serverVersion}", finding.server_mc_version)
                .replace("{modVersions}", finding.mod_mc_versions.join(", "))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AssignDialog({
  communityId,
  serverId,
  serverLoader,
  assignedIds,
  onSuccess,
  onClose,
}: {
  communityId: string;
  serverId: string;
  serverLoader: string;
  assignedIds: Set<string>;
  onSuccess: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const libraryQuery = useQuery({
    queryKey: ["mods"],
    queryFn: () => api.get("/api/mods"),
  });

  const assign = useMutation({
    mutationFn: () =>
      api.post(
        apiPath("/api/communities/{community_id}/servers/{server_id}/mods", {
          community_id: communityId,
          server_id: serverId,
        }),
        { body: JSON.stringify({ mod_ids: Array.from(selected) }) },
      ),
    onSuccess,
    onError: (error) => {
      if (onForbidden(error)) {
        onClose();
        return;
      }
      showToast(t(modsErrorMessage(error, "serverMods.assignError")), "error");
    },
  });

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  // Offer mods whose loader the server can run (LOADER_COMPAT, matching the
  // backend validation policy) that are not already assigned.
  const compatibleLoaders = LOADER_COMPAT[serverLoader] ?? [];
  const available: ModResponse[] = (libraryQuery.data?.mods ?? []).filter(
    (mod) =>
      compatibleLoaders.includes(mod.loader_type) && !assignedIds.has(mod.id),
  );

  return (
    <Modal
      open={true}
      title={t("serverMods.assignDialog.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={selected.size === 0 || assign.isPending}
            onClick={() => assign.mutate()}
          >
            {t("serverMods.assignDialog.submit")}
          </button>
        </>
      }
    >
      {libraryQuery.isPending ? (
        <p className="sub">{t("serverMods.assignDialog.loading")}</p>
      ) : available.length === 0 ? (
        <p className="sub">{t("serverMods.assignDialog.empty")}</p>
      ) : (
        <ul className="mod-pick-list">
          {available.map((mod) => (
            <li key={mod.id}>
              <label className="field-inline">
                <input
                  type="checkbox"
                  checked={selected.has(mod.id)}
                  onChange={() => toggle(mod.id)}
                />
                {mod.display_name} ({mod.version_number}){" "}
                <SideBadge side={mod.side} />
              </label>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
