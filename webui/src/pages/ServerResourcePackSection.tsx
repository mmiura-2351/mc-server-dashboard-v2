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
import { Modal } from "../components/Modal.tsx";
import { useToast } from "../components/Toast.tsx";
import { humanizeBytes } from "../format.ts";
import { type TranslationKey, t } from "../i18n/index.ts";
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

function assignErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    if (error.reason === "server_unsettled") {
      return "serverDetail.error.unsettled";
    }
    if (error.reason === "server_not_stopped") {
      return "serverDetail.error.notStopped";
    }
  }
  return "serverDetail.resourcePack.assignError";
}

function unassignErrorMessage(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    if (error.reason === "server_unsettled") {
      return "serverDetail.error.unsettled";
    }
    if (error.reason === "server_not_stopped") {
      return "serverDetail.error.notStopped";
    }
  }
  return "serverDetail.resourcePack.unassignError";
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
  const serverAtRest = atRest(
    normalizeState(server.observed_state),
    normalizeState(server.desired_state),
  );

  const assignmentQuery = useQuery({
    queryKey: assignmentKey(communityId, serverId),
    queryFn: async (): Promise<ResourcePackAssignmentResponse | null> => {
      try {
        const result = await api.get(
          apiPath(
            "/api/communities/{community_id}/servers/{server_id}/resource-pack",
            { community_id: communityId, server_id: serverId },
          ),
        );
        // Guard: the API returns 404 when no pack is assigned, caught below.
        // A non-assignment response (missing resource_pack) is treated as null.
        if (
          result === undefined ||
          typeof result !== "object" ||
          !("resource_pack" in (result as object))
        ) {
          return null;
        }
        return result as ResourcePackAssignmentResponse;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
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
      showToast(t(unassignErrorMessage(error)), "error");
    },
  });

  if (assignmentQuery.isPending) {
    return null;
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
          onChange={() => setAssignOpen(true)}
          onRemove={() => setRemoveOpen(true)}
        />
      )}
      {assignOpen && (
        <AssignDialog
          communityId={communityId}
          serverId={serverId}
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
  onChange,
  onRemove,
}: {
  assignment: ResourcePackAssignmentResponse;
  canUpdate: boolean;
  serverAtRest: boolean;
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
  onSuccess,
  onClose,
}: {
  communityId: string;
  serverId: string;
  onSuccess: () => void;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const [selectedId, setSelectedId] = useState("");
  const [require, setRequire] = useState(false);
  const [prompt, setPrompt] = useState("");

  const packsQuery = useQuery({
    queryKey: ["resource-packs"],
    queryFn: () => api.get("/api/resource-packs"),
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
            require_resource_pack: require,
            resource_pack_prompt: prompt.trim() || null,
          }),
        },
      ),
    onSuccess,
    onError: (error) => {
      if (onForbidden(error)) {
        onClose();
        return;
      }
      showToast(t(assignErrorMessage(error)), "error");
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
          <label className="field">
            <span className="field-inline">
              <input
                type="checkbox"
                checked={require}
                onChange={(e) => setRequire(e.target.checked)}
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
    </Modal>
  );
}
