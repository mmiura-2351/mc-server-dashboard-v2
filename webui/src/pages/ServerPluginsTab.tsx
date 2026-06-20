/**
 * Server detail — Plugins tab (issue #1153).
 *
 * Installed plugins list with enable/disable, remove, update actions. Local
 * jar upload for plugin installation. Modrinth catalog search and install with
 * version selector. Dependency viewer per plugin. Server state awareness
 * (read-only when running, mutations require stopped server). Full permission
 * gating (plugin:read, plugin:manage).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { downloadFile } from "../api/download.ts";
import { apiPath } from "../api/path.ts";
import {
  catalogProjectKey,
  catalogSearchKey,
  pluginsKey,
  pluginUpdatesKey,
  pluginValidationKey,
} from "../api/pluginQueryKeys.ts";
import type { components } from "../api/schema";
import { ConfirmDialog } from "../components/ConfirmDialog.tsx";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type PluginResponse = components["schemas"]["PluginResponse"];
type PluginUpdateInfoResponse =
  components["schemas"]["PluginUpdateInfoResponse"];
type CatalogSearchResultResponse =
  components["schemas"]["CatalogSearchResultResponse"];
type CatalogVersionResponse = components["schemas"]["CatalogVersionResponse"];
type PluginValidationResponse =
  components["schemas"]["PluginValidationResponse"];
type ResolutionPlanResponse = components["schemas"]["ResolutionPlanResponse"];
type ApplyResolutionResponse = components["schemas"]["ApplyResolutionResponse"];

/** Server types that support plugins/mods. Vanilla and Spigot do not. */
function supportsPlugins(serverType: string): boolean {
  return !["vanilla", "spigot"].includes(serverType);
}

function pluginErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.reason === "server_not_stopped") {
      return t("plugins.error.notStopped");
    }
  }
  return t("plugins.error.generic");
}

/** Map a manifest mod id to a friendly plugin name, falling back to the id. */
function nameOfPlugin(plugins: PluginResponse[], modId: string): string {
  const match = plugins.find((p) => p.mod_identifier === modId);
  return match?.display_name ?? modId;
}

/** Human label for a plugin side (issue #1308). */
function sideLabel(side: string): string {
  if (side === "server") return t("plugins.side.server");
  if (side === "client") return t("plugins.side.client");
  return t("plugins.side.both");
}

export function ServerPluginsTab({
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
  const fileInput = useRef<HTMLInputElement>(null);
  const [removeTarget, setRemoveTarget] = useState<PluginResponse | null>(null);
  const [browseOpen, setBrowseOpen] = useState(false);
  const [resolveOpen, setResolveOpen] = useState(false);

  const canRead = can("plugin:read", { serverId });
  const canManage = can("plugin:manage", { serverId });

  const state = normalizeState(server.observed_state);
  const desired = normalizeState(server.desired_state);
  const serverAtRest = atRest(state, desired);

  const onError = (error: unknown) => {
    if (onForbidden(error)) return;
    showToast(pluginErrorMessage(error), "error");
  };

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: pluginsKey(communityId, serverId),
    });
    queryClient.invalidateQueries({
      queryKey: pluginUpdatesKey(communityId, serverId),
    });
    queryClient.invalidateQueries({
      queryKey: pluginValidationKey(communityId, serverId),
    });
  };

  // -- Queries --

  const listQuery = useQuery({
    queryKey: pluginsKey(communityId, serverId),
    enabled: canRead,
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}/plugins", {
          community_id: communityId,
          server_id: serverId,
        }),
      ),
  });

  const updatesQuery = useQuery({
    queryKey: pluginUpdatesKey(communityId, serverId),
    enabled: canRead,
    queryFn: () =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/updates",
          { community_id: communityId, server_id: serverId },
        ),
      ),
  });

  const validationQuery = useQuery({
    queryKey: pluginValidationKey(communityId, serverId),
    enabled: canRead,
    queryFn: () =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/validate",
          { community_id: communityId, server_id: serverId },
        ),
      ),
  });

  // -- Mutations --

  const enableMutation = useMutation({
    mutationFn: (plugin: PluginResponse) =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/enable",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: plugin.id,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("plugins.enabled"), "success");
      refresh();
    },
    onError,
  });

  const disableMutation = useMutation({
    mutationFn: (plugin: PluginResponse) =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/disable",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: plugin.id,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("plugins.disabled"), "success");
      refresh();
    },
    onError,
  });

  const removeMutation = useMutation({
    mutationFn: (plugin: PluginResponse) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: plugin.id,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("plugins.removed"), "success");
      refresh();
    },
    onError,
  });

  const updateMutation = useMutation({
    mutationFn: ({
      plugin,
      versionId,
    }: {
      plugin: PluginResponse;
      versionId: string;
    }) =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/update",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: plugin.id,
          },
        ),
        { body: JSON.stringify({ version_id: versionId }) },
      ),
    onSuccess: () => {
      showToast(t("plugins.updated"), "success");
      refresh();
    },
    onError,
  });

  const sideMutation = useMutation({
    mutationFn: ({ plugin, side }: { plugin: PluginResponse; side: string }) =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/side",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: plugin.id,
          },
        ),
        { body: JSON.stringify({ side }) },
      ),
    onSuccess: () => {
      showToast(t("plugins.sideUpdated"), "success");
      refresh();
    },
    onError,
  });

  const downloadModpackMutation = useMutation({
    mutationFn: () =>
      downloadFile(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/client-mods/download",
          { community_id: communityId, server_id: serverId },
        ),
        "client-modpack.zip",
      ),
    onError,
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      form.append("display_name", file.name.replace(/\.jar$/i, ""));
      return api.postForm(
        apiPath("/api/communities/{community_id}/servers/{server_id}/plugins", {
          community_id: communityId,
          server_id: serverId,
        }),
        form,
      );
    },
    onSuccess: () => {
      showToast(t("plugins.installed"), "success");
      refresh();
    },
    onError,
  });

  // -- Guards --

  if (!supportsPlugins(server.server_type)) {
    return <p className="sub">{t("plugins.unsupported")}</p>;
  }
  if (!canRead) {
    return <p className="sub">{t("plugins.noRead")}</p>;
  }
  if (listQuery.isPending) {
    return <p className="sub">{t("plugins.loading")}</p>;
  }
  if (listQuery.isError) {
    return <p className="field-error">{t("plugins.loadError")}</p>;
  }

  const plugins = listQuery.data.plugins;
  const updates = updatesQuery.data?.updates ?? [];
  const updateMap = new Map(
    updates.map((u: PluginUpdateInfoResponse) => [u.plugin.id, u]),
  );
  const busy =
    enableMutation.isPending ||
    disableMutation.isPending ||
    removeMutation.isPending ||
    updateMutation.isPending ||
    sideMutation.isPending ||
    uploadMutation.isPending;
  // Client modpack = enabled plugins whose side is client-relevant.
  const hasClientMods = plugins.some(
    (p) => p.enabled && (p.side === "client" || p.side === "both"),
  );

  return (
    <section className="plugins">
      {!serverAtRest && (
        <p className="field-hint plugins-notice">
          {t("plugins.serverNotStopped")}
        </p>
      )}

      {canManage && (
        <div className="plugins-toolbar">
          <button
            type="button"
            className="btn primary"
            disabled={busy || !serverAtRest}
            onClick={() => fileInput.current?.click()}
          >
            {t("plugins.install")}
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".jar"
            hidden
            aria-label={t("plugins.install")}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file !== undefined) uploadMutation.mutate(file);
              e.target.value = "";
            }}
          />
          <button
            type="button"
            className="btn"
            disabled={busy || !serverAtRest}
            onClick={() => setBrowseOpen(true)}
          >
            {t("plugins.browse")}
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy || !serverAtRest}
            onClick={() => setResolveOpen(true)}
          >
            {t("plugins.resolve.action")}
          </button>
        </div>
      )}

      {hasClientMods && (
        <div className="plugins-toolbar">
          <button
            type="button"
            className="btn"
            disabled={downloadModpackMutation.isPending}
            onClick={() => downloadModpackMutation.mutate()}
          >
            {t("plugins.downloadClientModpack")}
          </button>
        </div>
      )}

      <div className="card plugins-table">
        <table className="data">
          <thead>
            <tr>
              <th>{t("plugins.col.name")}</th>
              <th>{t("plugins.col.version")}</th>
              <th>{t("plugins.col.source")}</th>
              <th>{t("plugins.col.side")}</th>
              <th>{t("plugins.col.status")}</th>
              <th>{t("plugins.col.size")}</th>
              {canManage && <th aria-label={t("plugins.col.actions")} />}
            </tr>
          </thead>
          <tbody>
            {plugins.length === 0 ? (
              <tr>
                <td colSpan={canManage ? 7 : 6} className="sub">
                  {t("plugins.empty")}
                </td>
              </tr>
            ) : (
              plugins.map((plugin) => {
                const update = updateMap.get(plugin.id);
                const hasUpdate =
                  update?.latest_version !== null &&
                  update?.latest_version !== undefined;
                return (
                  <PluginRow
                    key={plugin.id}
                    plugin={plugin}
                    hasUpdate={hasUpdate}
                    updateVersion={update?.latest_version ?? null}
                    canManage={canManage}
                    serverAtRest={serverAtRest}
                    busy={busy}
                    communityId={communityId}
                    serverId={serverId}
                    onEnable={() => enableMutation.mutate(plugin)}
                    onDisable={() => disableMutation.mutate(plugin)}
                    onRemove={() => setRemoveTarget(plugin)}
                    onUpdate={(versionId) =>
                      updateMutation.mutate({ plugin, versionId })
                    }
                    onSetSide={(side) => sideMutation.mutate({ plugin, side })}
                  />
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {validationQuery.data !== undefined && plugins.length > 0 && (
        <PluginValidationChecklist
          validation={validationQuery.data}
          nameOf={(modId) => nameOfPlugin(plugins, modId)}
        />
      )}

      <ConfirmDialog
        open={removeTarget !== null}
        title={t("plugins.removeDialog.title")}
        body={t("plugins.removeDialog.body")}
        confirmPhrase={t("plugins.removeDialog.phrase")}
        confirmLabel={t("plugins.removeDialog.confirm")}
        promptLabel={t("plugins.removeDialog.prompt")}
        onConfirm={() => {
          const target = removeTarget;
          setRemoveTarget(null);
          if (target !== null) removeMutation.mutate(target);
        }}
        onClose={() => setRemoveTarget(null)}
      />

      {browseOpen && (
        <ModrinthBrowser
          communityId={communityId}
          serverId={serverId}
          serverAtRest={serverAtRest}
          onDone={() => {
            setBrowseOpen(false);
            refresh();
          }}
          onClose={() => setBrowseOpen(false)}
          onError={onError}
        />
      )}

      {resolveOpen && (
        <PluginResolveModal
          communityId={communityId}
          serverId={serverId}
          serverAtRest={serverAtRest}
          nameOf={(modId) => nameOfPlugin(plugins, modId)}
          onApplied={() => {
            setResolveOpen(false);
            refresh();
          }}
          onClose={() => setResolveOpen(false)}
          onError={onError}
        />
      )}
    </section>
  );
}

// ── Plugin row with inline actions + dependency expander ──────────────────

function PluginRow({
  plugin,
  hasUpdate,
  updateVersion,
  canManage,
  serverAtRest,
  busy,
  communityId,
  serverId,
  onEnable,
  onDisable,
  onRemove,
  onUpdate,
  onSetSide,
}: {
  plugin: PluginResponse;
  hasUpdate: boolean;
  updateVersion: components["schemas"]["CatalogVersionItem"] | null;
  canManage: boolean;
  serverAtRest: boolean;
  busy: boolean;
  communityId: string;
  serverId: string;
  onEnable: () => void;
  onDisable: () => void;
  onRemove: () => void;
  onUpdate: (versionId: string) => void;
  onSetSide: (side: string) => void;
}) {
  const [depsOpen, setDepsOpen] = useState(false);

  return (
    <>
      <tr>
        <td>
          <strong>{plugin.display_name}</strong>
          {hasUpdate && updateVersion !== null && (
            <span className="pill plugins-update-badge">
              {t("plugins.updateAvailable")}
              {updateVersion.version_number}
            </span>
          )}
        </td>
        <td>{plugin.version_number ?? "—"}</td>
        <td>
          <span className="badge">
            {plugin.source === "modrinth"
              ? t("plugins.source.modrinth")
              : t("plugins.source.local")}
          </span>
        </td>
        <td>
          {canManage ? (
            <select
              className="plugins-side-select"
              aria-label={t("plugins.side.label")}
              value={plugin.side}
              disabled={busy || !serverAtRest}
              onChange={(e) => onSetSide(e.target.value)}
            >
              <option value="both">{t("plugins.side.both")}</option>
              <option value="server">{t("plugins.side.server")}</option>
              <option value="client">{t("plugins.side.client")}</option>
            </select>
          ) : (
            <span className="badge">{sideLabel(plugin.side)}</span>
          )}
        </td>
        <td>
          <span className={`pill ${plugin.enabled ? "running" : "stopped"}`}>
            {plugin.enabled
              ? t("plugins.status.enabled")
              : t("plugins.status.disabled")}
          </span>
        </td>
        <td className="num">
          {plugin.size_bytes !== null ? humanizeBytes(plugin.size_bytes) : "—"}
        </td>
        {canManage && (
          <td className="row-actions">
            <button
              type="button"
              className="btn sm"
              disabled={busy || !serverAtRest}
              onClick={plugin.enabled ? onDisable : onEnable}
            >
              {plugin.enabled ? t("plugins.disable") : t("plugins.enable")}
            </button>
            {hasUpdate && updateVersion !== null && (
              <button
                type="button"
                className="btn sm"
                disabled={busy || !serverAtRest}
                onClick={() => onUpdate(updateVersion.version_id)}
              >
                {t("plugins.update")}
              </button>
            )}
            <button
              type="button"
              className="btn sm danger"
              disabled={busy || !serverAtRest}
              onClick={onRemove}
            >
              {t("plugins.remove")}
            </button>
            {plugin.source === "modrinth" && (
              <button
                type="button"
                className="btn sm ghost"
                onClick={() => setDepsOpen((v) => !v)}
              >
                {t("plugins.dependencies")}
              </button>
            )}
          </td>
        )}
      </tr>
      {depsOpen && (
        <tr>
          <td colSpan={canManage ? 7 : 6}>
            <DependenciesView
              communityId={communityId}
              serverId={serverId}
              pluginId={plugin.id}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// ── Dependencies view ─────────────────────────────────────────────────────

function DependenciesView({
  communityId,
  serverId,
  pluginId,
}: {
  communityId: string;
  serverId: string;
  pluginId: string;
}) {
  const query = useQuery({
    queryKey: ["plugins", communityId, serverId, pluginId, "dependencies"],
    queryFn: () =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/{plugin_id}/dependencies",
          {
            community_id: communityId,
            server_id: serverId,
            plugin_id: pluginId,
          },
        ),
      ),
  });

  if (query.isPending) {
    return <p className="sub">{t("plugins.dependencies.loading")}</p>;
  }
  if (query.isError) {
    return <p className="field-error">{t("plugins.error.generic")}</p>;
  }
  const deps = query.data.dependencies;
  if (deps.length === 0) {
    return <p className="sub">{t("plugins.dependencies.empty")}</p>;
  }
  return (
    <ul className="plugins-deps">
      {deps.map((dep) => (
        <li key={dep.project_id}>
          <strong>
            {dep.project_title ?? dep.project_slug ?? dep.project_id}
          </strong>{" "}
          <span className="badge">
            {dep.dependency_type === "required"
              ? t("plugins.dependencies.required")
              : t("plugins.dependencies.optional")}
          </span>{" "}
          <span className={`pill ${dep.installed ? "running" : "crashed"}`}>
            {dep.installed
              ? t("plugins.dependencies.installed")
              : t("plugins.dependencies.missing")}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ── Dependency / compatibility validation checklist (issue #1307) ─────────

function PluginValidationChecklist({
  validation,
  nameOf,
}: {
  validation: PluginValidationResponse;
  nameOf: (modId: string) => string;
}) {
  const total =
    validation.missing_deps.length +
    validation.version_unsatisfied.length +
    validation.conflicts.length +
    validation.mc_mismatch.length;

  return (
    <div className="plugins-validation card">
      <h3>{t("plugins.validation.heading")}</h3>
      {total === 0 ? (
        <p className="field-hint">{t("plugins.validation.ok")}</p>
      ) : (
        <ul>
          {validation.missing_deps.map((finding) => (
            <li
              key={`dep-${finding.mod_id}-${finding.depends_on}`}
              className="field-error"
            >
              {t("plugins.validation.missingDep")
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
              {t("plugins.validation.versionUnsatisfied")
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
              {t("plugins.validation.conflict")
                .replace("{mod}", nameOf(finding.mod_id))
                .replace("{other}", finding.conflicts_with)}
            </li>
          ))}
          {validation.mc_mismatch.map((finding) => (
            <li key={`mc-${finding.mod_id}`} className="field-hint warn">
              {t("plugins.validation.mcMismatch")
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

// ── Dependency auto-resolution modal (issue #1309) ────────────────────────

function PluginResolveModal({
  communityId,
  serverId,
  serverAtRest,
  nameOf,
  onApplied,
  onClose,
  onError,
}: {
  communityId: string;
  serverId: string;
  serverAtRest: boolean;
  nameOf: (modId: string) => string;
  onApplied: () => void;
  onClose: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();

  // The plan is a POST (it queries Modrinth), so it is a mutation triggered on
  // open rather than a cached query. Read-only on the backend: nothing installs.
  const planMutation = useMutation({
    mutationFn: (): Promise<ResolutionPlanResponse> =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/resolve",
          { community_id: communityId, server_id: serverId },
        ),
      ),
    onError,
  });

  // Compute the plan once when the modal opens.
  const runPlan = planMutation.mutate;
  useEffect(() => {
    runPlan();
  }, [runPlan]);

  const applyMutation = useMutation({
    mutationFn: (): Promise<ApplyResolutionResponse> =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/plugins/resolve/apply",
          { community_id: communityId, server_id: serverId },
        ),
      ),
    onSuccess: (result) => {
      if (result.failed.length > 0) {
        showToast(t("plugins.resolve.appliedWithFailures"), "error");
      } else {
        showToast(t("plugins.resolve.applied"), "success");
      }
      onApplied();
    },
    onError,
  });

  const plan = planMutation.data;
  const entries = plan?.entries ?? [];
  const imports = entries.filter(
    (e) => e.status === "needs_import" && !e.blocked && e.will_import !== null,
  );
  const satisfied = entries.filter((e) => e.status === "already_satisfied");
  const conflicts = entries.filter((e) => e.blocked);
  const unresolvable = entries.filter(
    (e) => e.status === "unresolvable" && !e.blocked,
  );
  const busy = applyMutation.isPending;

  return (
    <Modal open={true} title={t("plugins.resolve.title")} onClose={onClose}>
      {planMutation.isPending ? (
        <p className="sub">{t("plugins.resolve.loading")}</p>
      ) : (
        <div className="plugins-resolve">
          {imports.length === 0 &&
            conflicts.length === 0 &&
            unresolvable.length === 0 && (
              <p className="field-hint">{t("plugins.resolve.nothing")}</p>
            )}

          {imports.length > 0 && (
            <section>
              <h4>{t("plugins.resolve.importsHeading")}</h4>
              <ul>
                {imports.map((e) => (
                  <li key={`import-${e.dep_identifier}`}>
                    {t("plugins.resolve.importItem")
                      .replace("{dependency}", e.dep_identifier)
                      .replace("{project}", e.will_import?.slug ?? "")
                      .replace(
                        "{version}",
                        e.will_import?.version_number ?? "",
                      )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {conflicts.length > 0 && (
            <section>
              <h4>{t("plugins.resolve.conflictsHeading")}</h4>
              <ul>
                {conflicts.map((e) => (
                  <li
                    key={`conflict-${e.dep_identifier}`}
                    className="field-error"
                  >
                    {t("plugins.resolve.conflictItem").replace(
                      "{dependency}",
                      e.dep_identifier,
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {unresolvable.length > 0 && (
            <section>
              <h4>{t("plugins.resolve.unresolvableHeading")}</h4>
              <ul>
                {unresolvable.map((e) => (
                  <li
                    key={`unresolvable-${e.dep_identifier}`}
                    className="field-hint warn"
                  >
                    {t("plugins.resolve.unresolvableItem").replace(
                      "{dependency}",
                      e.dep_identifier,
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {satisfied.length > 0 && (
            <section>
              <h4>{t("plugins.resolve.satisfiedHeading")}</h4>
              <ul>
                {satisfied.map((e) => (
                  <li key={`satisfied-${e.dep_identifier}`} className="sub">
                    {t("plugins.resolve.satisfiedItem").replace(
                      "{dependency}",
                      nameOf(e.dep_identifier),
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}

          <div className="plugins-resolve-actions">
            <button type="button" className="btn" onClick={onClose}>
              {t("plugins.resolve.cancel")}
            </button>
            <button
              type="button"
              className="btn primary"
              disabled={busy || !serverAtRest || imports.length === 0}
              onClick={() => applyMutation.mutate()}
            >
              {t("plugins.resolve.apply")}
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// ── Modrinth catalog browser modal ────────────────────────────────────────

function ModrinthBrowser({
  communityId,
  serverId,
  serverAtRest,
  onDone,
  onClose,
  onError,
}: {
  communityId: string;
  serverId: string;
  serverAtRest: boolean;
  onDone: () => void;
  onClose: () => void;
  onError: (error: unknown) => void;
}) {
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [selectedProject, setSelectedProject] = useState<string | null>(null);

  // Debounce: only search after user stops typing for 400ms.
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const handleSearchChange = (value: string) => {
    setSearchInput(value);
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setQuery(value.trim()), 400);
  };

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    };
  }, []);

  if (selectedProject !== null) {
    return (
      <ProjectDetailModal
        communityId={communityId}
        serverId={serverId}
        projectIdOrSlug={selectedProject}
        serverAtRest={serverAtRest}
        onBack={() => setSelectedProject(null)}
        onDone={onDone}
        onClose={onClose}
        onError={onError}
      />
    );
  }

  return (
    <Modal open={true} title={t("plugins.search.title")} onClose={onClose}>
      <input
        type="text"
        className="plugins-search-input"
        placeholder={t("plugins.search.placeholder")}
        aria-label={t("plugins.search.placeholder")}
        value={searchInput}
        onChange={(e) => handleSearchChange(e.target.value)}
      />
      {query.length > 0 && (
        <SearchResults
          communityId={communityId}
          serverId={serverId}
          query={query}
          onSelect={(projectId) => setSelectedProject(projectId)}
        />
      )}
    </Modal>
  );
}

function SearchResults({
  communityId,
  serverId,
  query,
  onSelect,
}: {
  communityId: string;
  serverId: string;
  query: string;
  onSelect: (projectId: string) => void;
}) {
  const searchQuery = useQuery({
    queryKey: catalogSearchKey(communityId, serverId, query),
    queryFn: async () => {
      const basePath = apiPath(
        "/api/communities/{community_id}/servers/{server_id}/catalog/search",
        { community_id: communityId, server_id: serverId },
      );
      const url = `${basePath}?q=${encodeURIComponent(query)}&limit=20`;
      return api.get(url as typeof basePath);
    },
  });

  if (searchQuery.isPending) {
    return <p className="sub">{t("plugins.loading")}</p>;
  }
  if (searchQuery.isError) {
    return <p className="field-error">{t("plugins.error.generic")}</p>;
  }
  const hits = searchQuery.data.hits;
  if (hits.length === 0) {
    return <p className="sub">{t("plugins.search.empty")}</p>;
  }
  return (
    <div className="plugins-search-results">
      {hits.map((hit: CatalogSearchResultResponse) => (
        <button
          key={hit.project_id}
          type="button"
          className="plugins-search-hit"
          onClick={() => onSelect(hit.project_id)}
        >
          {hit.icon_url !== null && (
            <img
              src={hit.icon_url}
              alt=""
              className="plugins-search-icon"
              width={40}
              height={40}
            />
          )}
          <div className="plugins-search-hit-info">
            <strong>{hit.title}</strong>
            <span className="sub">
              {t("plugins.search.by")} {hit.author} ·{" "}
              {hit.downloads.toLocaleString()} {t("plugins.search.downloads")}
            </span>
            <span className="sub">{hit.description}</span>
          </div>
        </button>
      ))}
    </div>
  );
}

// ── Project detail + version picker ───────────────────────────────────────

function ProjectDetailModal({
  communityId,
  serverId,
  projectIdOrSlug,
  serverAtRest,
  onBack,
  onDone,
  onClose,
  onError,
}: {
  communityId: string;
  serverId: string;
  projectIdOrSlug: string;
  serverAtRest: boolean;
  onBack: () => void;
  onDone: () => void;
  onClose: () => void;
  onError: (error: unknown) => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();

  const detailQuery = useQuery({
    queryKey: catalogProjectKey(communityId, serverId, projectIdOrSlug),
    queryFn: () =>
      api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/catalog/projects/{project_id_or_slug}",
          {
            community_id: communityId,
            server_id: serverId,
            project_id_or_slug: projectIdOrSlug,
          },
        ),
      ),
  });

  const installMutation = useMutation({
    mutationFn: (versionId: string) =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/catalog/install",
          { community_id: communityId, server_id: serverId },
        ),
        {
          body: JSON.stringify({
            project_id: projectIdOrSlug,
            version_id: versionId,
          }),
        },
      ),
    onSuccess: () => {
      showToast(t("plugins.catalogInstalled"), "success");
      onDone();
    },
    onError: (error) => {
      if (onForbidden(error)) return;
      onError(error);
    },
  });

  return (
    <Modal
      open={true}
      title={detailQuery.data?.project.title ?? t("plugins.search.versions")}
      onClose={onClose}
      footer={
        <button type="button" className="btn ghost" onClick={onBack}>
          {t("plugins.search.back")}
        </button>
      }
    >
      {detailQuery.isPending && <p className="sub">{t("plugins.loading")}</p>}
      {detailQuery.isError && (
        <p className="field-error">{t("plugins.error.generic")}</p>
      )}
      {detailQuery.data !== undefined && (
        <>
          <p className="sub">{detailQuery.data.project.description}</p>
          <h3>{t("plugins.search.versions")}</h3>
          <div className="plugins-versions">
            {detailQuery.data.versions.map(
              (version: CatalogVersionResponse) => (
                <div key={version.version_id} className="plugins-version-row">
                  <div>
                    <strong>{version.version_number}</strong>
                    {version.name !== version.version_number && (
                      <span className="sub"> {version.name}</span>
                    )}
                    <span className="sub">
                      {" "}
                      · {version.game_versions.join(", ")}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="btn sm primary"
                    disabled={installMutation.isPending || !serverAtRest}
                    onClick={() => installMutation.mutate(version.version_id)}
                  >
                    {installMutation.isPending
                      ? t("plugins.search.installing")
                      : t("plugins.search.install")}
                  </button>
                </div>
              ),
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
