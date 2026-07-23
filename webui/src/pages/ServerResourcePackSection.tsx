/**
 * Server detail — Resource pack assignment section (issue #1179).
 *
 * A card rendered in the Settings tab showing the current resource pack
 * assignment (or "none"), with assign/change/remove actions gated on
 * server:update and server-at-rest. The assign dialog fetches the library
 * list and lets the user pick a pack with optional require/prompt fields.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { copyToClipboard } from "../clipboard.ts";
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
import { supportsResourcePackOptions } from "../mcVersion.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";
import { atRest, normalizeState } from "./serverState.ts";

type ServerResponse = components["schemas"]["ServerResponse"];
type ResourcePackAssignmentResponse =
  components["schemas"]["ResourcePackAssignmentResponse"];
type ResourcePackResponse = components["schemas"]["ResourcePackResponse"];

function assignmentKey(communityId: string, serverId: string) {
  return ["resource-pack-assignment", communityId, serverId] as const;
}

function resourcePackErrorMessage(
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

export function ServerResourcePackSection({
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
  const [removeOpen, setRemoveOpen] = useState(false);

  const canUpdate = can("server:update", { serverId });
  const supportsRequirePrompt = supportsResourcePackOptions(server.mc_version);
  const serverAtRest = atRest(
    normalizeState(server.observed_state),
    normalizeState(server.desired_state),
  );

  const assignmentQuery = useQuery({
    queryKey: assignmentKey(communityId, serverId),
    queryFn: async ({
      signal,
    }): Promise<ResourcePackAssignmentResponse | null> => {
      // "No pack assigned" is a normal state of a valid server: the API returns
      // 200 with a null body, so no 404 special-casing is needed (issue #2238).
      const result = await api.get(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/resource-pack",
          { community_id: communityId, server_id: serverId },
        ),
        { signal },
      );
      // The body is either a full assignment or null; anything else is treated
      // as "unassigned".
      if (
        result !== null &&
        typeof result === "object" &&
        "resource_pack" in result
      ) {
        return result;
      }
      return null;
    },
  });

  const refresh = () => {
    queryClient.invalidateQueries({
      queryKey: assignmentKey(communityId, serverId),
    });
  };

  const unassign = useMutation({
    mutationFn: () =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/resource-pack",
          { community_id: communityId, server_id: serverId },
        ),
      ),
    onSuccess: () => {
      showToast(t("serverDetail.resourcePack.unassigned"), "success");
      setRemoveOpen(false);
      refresh();
    },
    onError: (error) => {
      setRemoveOpen(false);
      if (onForbidden(error)) {
        return;
      }
      showToast(
        t(
          resourcePackErrorMessage(
            error,
            "serverDetail.resourcePack.unassignError",
          ),
        ),
        "error",
      );
    },
  });

  if (assignmentQuery.isPending) {
    return null;
  }

  // Error only when there is nothing to show (the initial load failed). A
  // failed background refetch retains `data`, so the cached assignment keeps
  // rendering through transient API blips (#1985).
  if (assignmentQuery.data === undefined) {
    return (
      <div className="card form-card">
        <h2>{t("serverDetail.resourcePack.heading")}</h2>
        <p className="field-error">
          {t("serverDetail.resourcePack.loadError")}
        </p>
      </div>
    );
  }

  const assignment = assignmentQuery.data ?? null;

  return (
    <div className="card form-card">
      <h2>{t("serverDetail.resourcePack.heading")}</h2>
      {assignment === null ? (
        <UnassignedView
          canUpdate={canUpdate}
          serverAtRest={serverAtRest}
          onAssign={() => setAssignOpen(true)}
        />
      ) : (
        <AssignedView
          assignment={assignment}
          canUpdate={canUpdate}
          serverAtRest={serverAtRest}
          supportsRequirePrompt={supportsRequirePrompt}
          onChange={() => setAssignOpen(true)}
          onRemove={() => setRemoveOpen(true)}
        />
      )}
      {assignOpen && (
        <AssignDialog
          communityId={communityId}
          serverId={serverId}
          initialPackId={assignment?.resource_pack.id}
          initialRequire={assignment?.require_resource_pack}
          initialPrompt={assignment?.resource_pack_prompt ?? undefined}
          supportsRequirePrompt={supportsRequirePrompt}
          onSuccess={() => {
            setAssignOpen(false);
            showToast(t("serverDetail.resourcePack.assigned"), "success");
            refresh();
          }}
          onClose={() => setAssignOpen(false)}
        />
      )}
      <Modal
        open={removeOpen}
        title={t("serverDetail.resourcePack.removeDialog.title")}
        onClose={() => setRemoveOpen(false)}
        footer={
          <>
            <button
              type="button"
              className="btn ghost"
              onClick={() => setRemoveOpen(false)}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={unassign.isPending}
              onClick={() => unassign.mutate()}
            >
              {t("serverDetail.resourcePack.removeDialog.confirm")}
            </button>
          </>
        }
      >
        <p>{t("serverDetail.resourcePack.removeDialog.body")}</p>
      </Modal>
    </div>
  );
}

function UnassignedView({
  canUpdate,
  serverAtRest,
  onAssign,
}: {
  canUpdate: boolean;
  serverAtRest: boolean;
  onAssign: () => void;
}) {
  return (
    <>
      <p className="sub">{t("serverDetail.resourcePack.none")}</p>
      {canUpdate && (
        <>
          <button
            type="button"
            className="btn primary"
            disabled={!serverAtRest}
            onClick={onAssign}
          >
            {t("serverDetail.resourcePack.assign")}
          </button>
          {!serverAtRest && (
            <p className="field-hint">
              {t("serverDetail.resourcePack.notAtRest")}
            </p>
          )}
        </>
      )}
    </>
  );
}

function AssignedView({
  assignment,
  canUpdate,
  serverAtRest,
  supportsRequirePrompt,
  onChange,
  onRemove,
}: {
  assignment: ResourcePackAssignmentResponse;
  canUpdate: boolean;
  serverAtRest: boolean;
  supportsRequirePrompt: boolean;
  onChange: () => void;
  onRemove: () => void;
}) {
  const pack = assignment.resource_pack;

  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    };
  }, []);
  const handleCopy = useCallback(() => {
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    copyToClipboard(pack.download_url).then(
      () => {
        setCopied(true);
        copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
      },
      () => {
        setCopied(false);
      },
    );
  }, [pack.download_url]);

  return (
    <>
      <dl className="kv">
        <dt>{t("serverDetail.resourcePack.name")}</dt>
        <dd>{pack.display_name}</dd>
        <dt>{t("serverDetail.resourcePack.filename")}</dt>
        <dd>{pack.filename}</dd>
        <dt>{t("serverDetail.resourcePack.size")}</dt>
        <dd>{humanizeBytes(pack.size_bytes)}</dd>
        <dt>{t("serverDetail.resourcePack.sha1")}</dt>
        <dd title={pack.sha1_hash}>{pack.sha1_hash}</dd>
        <dt>{t("serverDetail.resourcePack.url")}</dt>
        <dd>
          <button
            type="button"
            className="badge copyable"
            title={pack.download_url}
            onClick={handleCopy}
          >
            {copied
              ? t("serverDetail.resourcePack.urlCopied")
              : pack.download_url}
          </button>
        </dd>
        {supportsRequirePrompt && (
          <>
            <dt>{t("serverDetail.resourcePack.required")}</dt>
            <dd>
              {t(
                assignment.require_resource_pack
                  ? "serverDetail.resourcePack.required"
                  : "serverDetail.resourcePack.notRequired",
              )}
            </dd>
            <dt>{t("serverDetail.resourcePack.prompt")}</dt>
            <dd>
              {assignment.resource_pack_prompt ??
                t("serverDetail.resourcePack.promptNone")}
            </dd>
          </>
        )}
      </dl>
      {canUpdate && (
        <div className="actions">
          <button
            type="button"
            className="btn"
            disabled={!serverAtRest}
            onClick={onChange}
          >
            {t("serverDetail.resourcePack.change")}
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={!serverAtRest}
            onClick={onRemove}
          >
            {t("serverDetail.resourcePack.remove")}
          </button>
          {!serverAtRest && (
            <p className="field-hint">
              {t("serverDetail.resourcePack.notAtRest")}
            </p>
          )}
        </div>
      )}
    </>
  );
}

function AssignDialog({
  communityId,
  serverId,
  initialPackId,
  initialRequire,
  initialPrompt,
  supportsRequirePrompt,
  onSuccess,
  onClose,
}: {
  communityId: string;
  serverId: string;
  initialPackId?: string;
  initialRequire?: boolean;
  initialPrompt?: string;
  supportsRequirePrompt: boolean;
  onSuccess: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const [selectedId, setSelectedId] = useState(initialPackId ?? "");
  const [requirePack, setRequirePack] = useState(initialRequire ?? false);
  const [prompt, setPrompt] = useState(initialPrompt ?? "");

  const packsQuery = useQuery({
    queryKey: ["resource-packs"],
    queryFn: ({ signal }) => api.get("/api/resource-packs", { signal }),
  });

  const assign = useMutation({
    mutationFn: () =>
      api.post(
        apiPath(
          "/api/communities/{community_id}/servers/{server_id}/resource-pack",
          { community_id: communityId, server_id: serverId },
        ),
        {
          body: JSON.stringify({
            resource_pack_id: selectedId,
            require_resource_pack: supportsRequirePrompt ? requirePack : false,
            resource_pack_prompt: supportsRequirePrompt
              ? prompt.trim() || null
              : null,
          }),
        },
      ),
    onSuccess,
    onError: (error) => {
      if (onForbidden(error)) {
        onClose();
        return;
      }
      showToast(
        t(
          resourcePackErrorMessage(
            error,
            "serverDetail.resourcePack.assignError",
          ),
        ),
        "error",
      );
    },
  });

  const packs: ResourcePackResponse[] = packsQuery.data?.resource_packs ?? [];

  return (
    <Modal
      open={true}
      title={t("serverDetail.resourcePack.assignDialog.title")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={selectedId === "" || assign.isPending}
            onClick={() => assign.mutate()}
          >
            {t("serverDetail.resourcePack.assignDialog.submit")}
          </button>
        </>
      }
    >
      {packsQuery.isPending ? (
        <p className="sub">
          {t("serverDetail.resourcePack.assignDialog.loading")}
        </p>
      ) : packs.length === 0 ? (
        <p className="sub">
          {t("serverDetail.resourcePack.assignDialog.empty")}
        </p>
      ) : (
        <>
          <label className="field">
            {t("serverDetail.resourcePack.assignDialog.select")}
            <select
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
            >
              <option value="">
                {t("serverDetail.resourcePack.assignDialog.selectPlaceholder")}
              </option>
              {packs.map((pack) => (
                <option key={pack.id} value={pack.id}>
                  {pack.display_name} ({humanizeBytes(pack.size_bytes)})
                </option>
              ))}
            </select>
          </label>
          {supportsRequirePrompt && (
            <>
              <label className="field">
                <span className="field-inline">
                  <input
                    type="checkbox"
                    checked={requirePack}
                    onChange={(e) => setRequirePack(e.target.checked)}
                  />
                  {t("serverDetail.resourcePack.assignDialog.require")}
                </span>
              </label>
              <label className="field">
                {t("serverDetail.resourcePack.assignDialog.prompt")}
                <input
                  type="text"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </label>
            </>
          )}
        </>
      )}
    </Modal>
  );
}
