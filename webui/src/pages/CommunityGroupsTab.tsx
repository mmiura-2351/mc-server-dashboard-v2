import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useState } from "react";
import { api } from "../api/client.ts";
import { attachmentsKeys, groupsKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { Modal } from "../components/Modal.tsx";
import { SimpleConfirmDialog } from "../components/SimpleConfirmDialog.tsx";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type GroupResponse = components["schemas"]["GroupResponse"];
type PlayerResponse = components["schemas"]["PlayerResponse"];
type ServerResponse = components["schemas"]["ServerResponse"];

// `kind` is a free-form string on the wire; only op/whitelist have a localized
// label (matching ServerPlayersTab), anything else falls back to its raw value.
function kindLabel(kind: string): string {
  if (kind === "op") {
    return t("communitySettings.groups.kind.op");
  }
  if (kind === "whitelist") {
    return t("communitySettings.groups.kind.whitelist");
  }
  return kind;
}

const KIND_CLASS: Record<string, string> = {
  op: "op",
  whitelist: "whitelist",
};

// Groups tab (WEBUI_SPEC.md 6.10): op/whitelist groups with create/rename/
// delete; an expandable detail panel per group manages its players (uuid +
// username) and its server attachments. Reads gate on `group:read`, every
// mutation on `group:manage`.
export function CommunityGroupsTab({
  communityId,
  can,
}: {
  communityId: string;
  can: Can;
}) {
  const canManage = can("group:manage");
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const [createOpen, setCreateOpen] = useState(false);
  const [renaming, setRenaming] = useState<GroupResponse | null>(null);
  const [deleting, setDeleting] = useState<GroupResponse | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const groups = useQuery({
    queryKey: groupsKeys.list(communityId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/groups", {
          community_id: communityId,
        }),
      ),
  });

  // Community servers feed the attach picker; only needed to manage attachments.
  const servers = useQuery({
    queryKey: ["communities", communityId, "servers"],
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers", {
          community_id: communityId,
        }),
      ),
    enabled: canManage,
  });

  const invalidateGroups = () =>
    queryClient.invalidateQueries({ queryKey: groupsKeys.list(communityId) });

  const remove = useMutation({
    mutationFn: (group: GroupResponse) =>
      api.delete(
        apiPath("/api/communities/{community_id}/groups/{group_id}", {
          community_id: communityId,
          group_id: group.id,
        }),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.groups.deleted"), "success");
      invalidateGroups();
      // A deleted group also disappears from each server's attached-group list,
      // which ServerPlayersTab reads under the attachments prefix (#473).
      queryClient.invalidateQueries({
        queryKey: attachmentsKeys.all(communityId),
      });
      setDeleting(null);
    },
    onError: (error) => {
      setDeleting(null);
      if (onForbidden(error)) {
        return;
      }
      showToast(t("communitySettings.groups.error"), "error");
    },
  });

  if (groups.isPending) {
    return <p className="sub">{t("communitySettings.groups.loading")}</p>;
  }
  if (groups.isError || groups.data === undefined) {
    return (
      <p className="field-error">{t("communitySettings.groups.loadError")}</p>
    );
  }

  return (
    <section className="groups">
      <div className="page-head">
        <h2>{t("communitySettings.groups.heading")}</h2>
        {canManage && (
          <button
            type="button"
            className="btn primary"
            onClick={() => setCreateOpen(true)}
          >
            {t("communitySettings.groups.create")}
          </button>
        )}
      </div>

      {groups.data.length === 0 ? (
        <p className="sub">{t("communitySettings.groups.empty")}</p>
      ) : (
        <ul className="group-list">
          {groups.data.map((group) => (
            <li className="group-item" key={group.id}>
              <div className="group-row">
                <span className="group-name">{group.name}</span>
                <span className={`badge kind ${KIND_CLASS[group.kind] ?? ""}`}>
                  {kindLabel(group.kind)}
                </span>
                <span className="sub group-members">
                  {group.players.length}{" "}
                  {t("communitySettings.groups.memberCount")}
                </span>
                <button
                  type="button"
                  className="btn sm"
                  onClick={() =>
                    setExpanded((id) => (id === group.id ? null : group.id))
                  }
                >
                  {expanded === group.id
                    ? t("communitySettings.groups.collapse")
                    : t("communitySettings.groups.expand")}
                </button>
                {canManage && (
                  <>
                    <button
                      type="button"
                      className="btn sm ghost"
                      onClick={() => setRenaming(group)}
                    >
                      {t("communitySettings.groups.rename")}
                    </button>
                    <button
                      type="button"
                      className="btn sm danger"
                      onClick={() => setDeleting(group)}
                    >
                      {t("communitySettings.groups.delete")}
                    </button>
                  </>
                )}
              </div>
              {expanded === group.id && (
                <GroupDetail
                  communityId={communityId}
                  group={group}
                  servers={servers.data ?? []}
                  canManage={canManage}
                />
              )}
            </li>
          ))}
        </ul>
      )}

      {canManage && (
        <CreateGroupDialog
          open={createOpen}
          communityId={communityId}
          onClose={() => setCreateOpen(false)}
        />
      )}
      {canManage && renaming !== null && (
        <RenameGroupDialog
          communityId={communityId}
          group={renaming}
          onClose={() => setRenaming(null)}
        />
      )}

      <SimpleConfirmDialog
        open={deleting !== null}
        title={t("communitySettings.groups.deleteDialogTitle")}
        body={t("communitySettings.groups.deleteDialogBody")}
        confirmLabel={t("communitySettings.groups.deleteConfirm")}
        busy={remove.isPending}
        onConfirm={() => {
          if (deleting !== null) {
            remove.mutate(deleting);
          }
        }}
        onClose={() => setDeleting(null)}
      />
    </section>
  );
}

function GroupDetail({
  communityId,
  group,
  servers,
  canManage,
}: {
  communityId: string;
  group: GroupResponse;
  servers: ServerResponse[];
  canManage: boolean;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [removingPlayer, setRemovingPlayer] = useState<PlayerResponse | null>(
    null,
  );

  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t("communitySettings.groups.error"), "error");
  };
  const invalidateGroups = () =>
    queryClient.invalidateQueries({ queryKey: groupsKeys.list(communityId) });
  // Attach/detach and player add/remove both change the relation rendered from
  // both ends, so invalidate the whole attachment prefix: this refreshes this
  // group's server list here and the server's group list (with its
  // group.players.length count) in ServerPlayersTab wherever it is mounted
  // (#473, #611).
  const invalidateAttachments = () =>
    queryClient.invalidateQueries({
      queryKey: attachmentsKeys.all(communityId),
    });

  const attachedServers = useQuery({
    queryKey: attachmentsKeys.forGroup(communityId, group.id),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/groups/{group_id}/servers", {
          community_id: communityId,
          group_id: group.id,
        }),
      ),
  });

  const removePlayer = useMutation({
    mutationFn: (uuid: string) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/groups/{group_id}/players/{player_uuid}",
          {
            community_id: communityId,
            group_id: group.id,
            player_uuid: uuid,
          },
        ),
      ),
    onSuccess: () => {
      showToast(t("communitySettings.groups.playerRemoved"), "success");
      setRemovingPlayer(null);
    },
    onSettled: () => {
      invalidateGroups();
      invalidateAttachments();
    },
    onError: (error) => {
      setRemovingPlayer(null);
      onError(error);
    },
  });

  const attach = useMutation({
    mutationFn: (serverId: string) =>
      api.put(
        apiPath(
          "/api/communities/{community_id}/groups/{group_id}/servers/{server_id}",
          {
            community_id: communityId,
            group_id: group.id,
            server_id: serverId,
          },
        ),
      ),
    onSuccess: () =>
      showToast(t("communitySettings.groups.attached"), "success"),
    onSettled: invalidateAttachments,
    onError,
  });

  const detach = useMutation({
    mutationFn: (serverId: string) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/groups/{group_id}/servers/{server_id}",
          {
            community_id: communityId,
            group_id: group.id,
            server_id: serverId,
          },
        ),
      ),
    onSuccess: () =>
      showToast(t("communitySettings.groups.detached"), "success"),
    onSettled: invalidateAttachments,
    onError,
  });

  const serverById = new Map(servers.map((s) => [s.id, s]));
  const attachedIds = attachedServers.data ?? [];
  const attachedSet = new Set(attachedIds);
  // Picker = community servers not already attached.
  const candidates = servers.filter((s) => !attachedSet.has(s.id));
  const serverBusy = attach.isPending || detach.isPending;

  return (
    <div className="group-detail">
      <div className="card">
        <h3>{t("communitySettings.groups.playersHeading")}</h3>
        {group.players.length === 0 ? (
          <p className="sub">{t("communitySettings.groups.playersEmpty")}</p>
        ) : (
          <ul className="player-list">
            {group.players.map((player) => (
              <PlayerRow
                key={player.uuid}
                player={player}
                action={
                  canManage ? (
                    <button
                      type="button"
                      className="btn sm danger"
                      disabled={removePlayer.isPending}
                      onClick={() => setRemovingPlayer(player)}
                    >
                      {t("communitySettings.groups.removePlayer")}
                    </button>
                  ) : null
                }
              />
            ))}
          </ul>
        )}
        {canManage && (
          <AddPlayerForm communityId={communityId} groupId={group.id} />
        )}
      </div>

      <div className="card">
        <h3>{t("communitySettings.groups.serversHeading")}</h3>
        {attachedServers.isPending ? (
          <p className="sub">{t("communitySettings.groups.serversLoading")}</p>
        ) : attachedServers.isError || attachedServers.data === undefined ? (
          <p className="field-error">
            {t("communitySettings.groups.serversLoadError")}
          </p>
        ) : attachedIds.length === 0 ? (
          <p className="sub">{t("communitySettings.groups.serversEmpty")}</p>
        ) : (
          <ul className="server-list">
            {attachedIds.map((serverId) => (
              <li className="server-row" key={serverId}>
                <span className="server-name">
                  {serverById.get(serverId)?.name ??
                    t("communitySettings.groups.unknownServer")}
                </span>
                {canManage && (
                  <button
                    type="button"
                    className="btn sm danger"
                    disabled={serverBusy}
                    onClick={() => detach.mutate(serverId)}
                  >
                    {t("communitySettings.groups.detach")}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}

        {canManage && (
          <div className="group-attach">
            <h4>{t("communitySettings.groups.attachHeading")}</h4>
            {candidates.length === 0 ? (
              <p className="sub">{t("communitySettings.groups.attachEmpty")}</p>
            ) : (
              <ul className="server-list">
                {candidates.map((server) => (
                  <li className="server-row" key={server.id}>
                    <span className="server-name">{server.name}</span>
                    <button
                      type="button"
                      className="btn sm"
                      disabled={serverBusy}
                      onClick={() => attach.mutate(server.id)}
                    >
                      {t("communitySettings.groups.attach")}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>

      <SimpleConfirmDialog
        open={removingPlayer !== null}
        title={t("communitySettings.groups.removePlayerDialogTitle")}
        body={t("communitySettings.groups.removePlayerDialogBody")}
        confirmLabel={t("communitySettings.groups.removePlayerConfirm")}
        busy={removePlayer.isPending}
        onConfirm={() => {
          if (removingPlayer !== null) {
            removePlayer.mutate(removingPlayer.uuid);
          }
        }}
        onClose={() => setRemovingPlayer(null)}
      />
    </div>
  );
}

function PlayerRow({
  player,
  action,
}: {
  player: PlayerResponse;
  action: ReactNode;
}) {
  return (
    <li className="player-row">
      <span className="player-name">{player.username}</span>
      <span className="sub player-uuid">{player.uuid}</span>
      {action}
    </li>
  );
}

function AddPlayerForm({
  communityId,
  groupId,
}: {
  communityId: string;
  groupId: string;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [uuid, setUuid] = useState("");
  const [username, setUsername] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = useMutation({
    mutationFn: (body: { uuid: string; username: string }) =>
      api.post(
        apiPath("/api/communities/{community_id}/groups/{group_id}/players", {
          community_id: communityId,
          group_id: groupId,
        }),
        { body: JSON.stringify(body) },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.groups.playerAdded"), "success");
      queryClient.invalidateQueries({ queryKey: groupsKeys.list(communityId) });
      // Refresh the server's-groups projection too so the Players tab's
      // group.players.length count stops being stale (#611).
      queryClient.invalidateQueries({
        queryKey: attachmentsKeys.all(communityId),
      });
      setUuid("");
      setUsername("");
      setError(null);
    },
    onError: (err) => {
      if (onForbidden(err)) {
        return;
      }
      setError(t("communitySettings.groups.error"));
    },
  });

  const submit = () => {
    const trimmedUuid = uuid.trim();
    const trimmedName = username.trim();
    if (trimmedUuid.length === 0 || trimmedName.length === 0) {
      setError(t("communitySettings.groups.playerFieldsEmpty"));
      return;
    }
    setError(null);
    add.mutate({ uuid: trimmedUuid, username: trimmedName });
  };

  return (
    <div className="group-add-player">
      <label className="field">
        {t("communitySettings.groups.uuidLabel")}
        <input
          type="text"
          value={uuid}
          placeholder={t("communitySettings.groups.uuidPlaceholder")}
          onChange={(e) => setUuid(e.target.value)}
        />
      </label>
      <label className="field">
        {t("communitySettings.groups.usernameLabel")}
        <input
          type="text"
          value={username}
          placeholder={t("communitySettings.groups.usernamePlaceholder")}
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn"
        disabled={add.isPending}
        onClick={submit}
      >
        {t("communitySettings.groups.addPlayer")}
      </button>
      {error !== null && <span className="field-error">{error}</span>}
    </div>
  );
}

function CreateGroupDialog({
  open,
  communityId,
  onClose,
}: {
  open: boolean;
  communityId: string;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("op");
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setName("");
    setKind("op");
    setError(null);
    onClose();
  };

  const create = useMutation({
    mutationFn: (body: { name: string; kind: string }) =>
      api.post(
        apiPath("/api/communities/{community_id}/groups", {
          community_id: communityId,
        }),
        { body: JSON.stringify(body) },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.groups.created"), "success");
      queryClient.invalidateQueries({ queryKey: groupsKeys.list(communityId) });
      close();
    },
    onError: (err) => {
      if (onForbidden(err)) {
        close();
        return;
      }
      setError(t("communitySettings.groups.error"));
    },
  });

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setError(t("communitySettings.groups.nameEmpty"));
      return;
    }
    setError(null);
    create.mutate({ name: trimmed, kind });
  };

  return (
    <Modal
      open={open}
      title={t("communitySettings.groups.createDialogTitle")}
      onClose={close}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={close}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={create.isPending}
            onClick={submit}
          >
            {t("communitySettings.groups.createSubmit")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("communitySettings.groups.nameLabel")}
        <input
          type="text"
          value={name}
          placeholder={t("communitySettings.groups.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="field">
        {t("communitySettings.groups.kindLabel")}
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="op">{t("communitySettings.groups.kind.op")}</option>
          <option value="whitelist">
            {t("communitySettings.groups.kind.whitelist")}
          </option>
        </select>
      </label>
      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}

function RenameGroupDialog({
  communityId,
  group,
  onClose,
}: {
  communityId: string;
  group: GroupResponse;
  onClose: () => void;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();
  const [name, setName] = useState(group.name);
  const [error, setError] = useState<string | null>(null);

  const rename = useMutation({
    mutationFn: (newName: string) =>
      api.patch(
        apiPath("/api/communities/{community_id}/groups/{group_id}", {
          community_id: communityId,
          group_id: group.id,
        }),
        { body: JSON.stringify({ name: newName }) },
      ),
    onSuccess: () => {
      showToast(t("communitySettings.groups.renamed"), "success");
      queryClient.invalidateQueries({ queryKey: groupsKeys.list(communityId) });
      onClose();
    },
    onError: (err) => {
      if (onForbidden(err)) {
        onClose();
        return;
      }
      setError(t("communitySettings.groups.error"));
    },
  });

  const submit = () => {
    const trimmed = name.trim();
    if (trimmed.length === 0) {
      setError(t("communitySettings.groups.nameEmpty"));
      return;
    }
    setError(null);
    rename.mutate(trimmed);
  };

  return (
    <Modal
      open={true}
      title={t("communitySettings.groups.renameDialogTitle")}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn ghost" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={rename.isPending}
            onClick={submit}
          >
            {t("communitySettings.groups.renameSubmit")}
          </button>
        </>
      }
    >
      <label className="field">
        {t("communitySettings.groups.nameLabel")}
        <input
          type="text"
          value={name}
          placeholder={t("communitySettings.groups.namePlaceholder")}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {error !== null && <span className="field-error">{error}</span>}
    </Modal>
  );
}
