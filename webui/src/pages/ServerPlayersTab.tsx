import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { api } from "../api/client.ts";
import { attachmentsKeys, groupsKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type GroupResponse = components["schemas"]["GroupResponse"];

// `kind` is a free-form string on the wire; only op/whitelist have a localized
// label, anything else falls back to its raw value.
function kindLabel(kind: string): string {
  if (kind === "op") {
    return t("players.kind.op");
  }
  if (kind === "whitelist") {
    return t("players.kind.whitelist");
  }
  return kind;
}

export function ServerPlayersTab({
  communityId,
  serverId,
  can,
}: {
  communityId: string;
  serverId: string;
  can: Can;
}) {
  const { showToast } = useToast();
  const onForbidden = useOnForbidden();
  const queryClient = useQueryClient();

  const canManage = can("group:manage");

  const attached = useQuery({
    queryKey: attachmentsKeys.forServer(communityId, serverId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/servers/{server_id}/groups", {
          community_id: communityId,
          server_id: serverId,
        }),
      ),
  });

  // Source for the attach picker; only needed to manage attachments.
  const community = useQuery({
    queryKey: groupsKeys.list(communityId),
    queryFn: () =>
      api.get(
        apiPath("/api/communities/{community_id}/groups", {
          community_id: communityId,
        }),
      ),
    enabled: canManage,
  });

  // Attach/detach changes the relation from both ends, so invalidate the whole
  // attachment prefix: this refreshes this server's group list here and the
  // group's server list in CommunityGroupsTab wherever it is mounted (#473).
  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: attachmentsKeys.all(communityId),
    });
  const onError = (error: unknown) => {
    if (onForbidden(error)) {
      return;
    }
    showToast(t("players.error.generic"), "error");
  };

  const attach = useMutation({
    mutationFn: (groupId: string) =>
      api.put(
        apiPath(
          "/api/communities/{community_id}/groups/{group_id}/servers/{server_id}",
          {
            community_id: communityId,
            group_id: groupId,
            server_id: serverId,
          },
        ),
      ),
    onSuccess: () => showToast(t("players.attached"), "success"),
    onSettled: invalidate,
    onError,
  });

  const detach = useMutation({
    mutationFn: (groupId: string) =>
      api.delete(
        apiPath(
          "/api/communities/{community_id}/groups/{group_id}/servers/{server_id}",
          {
            community_id: communityId,
            group_id: groupId,
            server_id: serverId,
          },
        ),
      ),
    onSuccess: () => showToast(t("players.detached"), "success"),
    onSettled: invalidate,
    onError,
  });

  if (attached.isPending) {
    return <p className="sub">{t("players.loading")}</p>;
  }
  if (attached.isError || attached.data === undefined) {
    return <p className="field-error">{t("players.loadError")}</p>;
  }

  const attachedGroups = attached.data;
  const attachedIds = new Set(attachedGroups.map((g) => g.id));
  const communityGroups = community.data ?? [];
  // Picker = community groups not already attached (WEBUI_SPEC.md 6.8).
  const candidates = communityGroups.filter((g) => !attachedIds.has(g.id));
  const busy = attach.isPending || detach.isPending;

  return (
    <section className="players">
      <div className="card">
        <h2>{t("players.heading")}</h2>
        {attachedGroups.length === 0 ? (
          <p className="sub">{t("players.empty")}</p>
        ) : (
          <ul className="group-list">
            {attachedGroups.map((group) => (
              <GroupRow
                key={group.id}
                group={group}
                action={
                  canManage ? (
                    <button
                      type="button"
                      className="btn sm danger"
                      disabled={busy}
                      onClick={() => detach.mutate(group.id)}
                    >
                      {t("players.detach")}
                    </button>
                  ) : null
                }
              />
            ))}
          </ul>
        )}
      </div>

      {canManage && (
        <div className="card">
          <h2>{t("players.attachHeading")}</h2>
          {candidates.length === 0 ? (
            <p className="sub">
              {communityGroups.length === 0
                ? t("players.attachNoGroups")
                : t("players.attachEmpty")}
            </p>
          ) : (
            <ul className="group-list">
              {candidates.map((group) => (
                <GroupRow
                  key={group.id}
                  group={group}
                  action={
                    <button
                      type="button"
                      className="btn sm"
                      disabled={busy}
                      onClick={() => attach.mutate(group.id)}
                    >
                      {t("players.attach")}
                    </button>
                  }
                />
              ))}
            </ul>
          )}
        </div>
      )}

      <p className="field-hint">
        {t("players.manageHint")}{" "}
        <Link to={`/communities/${communityId}/settings`}>
          {t("players.manageLink")}
        </Link>
      </p>
    </section>
  );
}

function GroupRow({
  group,
  action,
}: {
  group: GroupResponse;
  action: ReactNode;
}) {
  const kindClass: Record<string, string> = {
    op: "op",
    whitelist: "whitelist",
  };
  return (
    <li className="group-row">
      <span className="group-name">{group.name}</span>
      <span className={`badge kind ${kindClass[group.kind] ?? ""}`}>
        {kindLabel(group.kind)}
      </span>
      <span className="sub group-members">
        {group.players.length} {t("players.memberCount")}
      </span>
      {action}
    </li>
  );
}
