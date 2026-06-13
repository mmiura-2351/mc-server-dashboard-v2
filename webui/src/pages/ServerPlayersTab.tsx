import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client.ts";
import { attachmentsKeys, groupsKeys } from "../api/communityQueryKeys.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { useToast } from "../components/Toast.tsx";
import { formatDateTime } from "../format.ts";
import { t } from "../i18n/index.ts";
import type { Can } from "../permissions/useCan.ts";
import { useOnForbidden } from "../permissions/useOnForbidden.ts";

type GroupResponse = components["schemas"]["GroupResponse"];
type GameSessionResponse = components["schemas"]["GameSessionResponse"];

const SESSIONS_PAGE_SIZE = 20;

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
          {community.isPending ? (
            // Until the community groups list resolves, the empty-state copy
            // below would misreport "no groups" (community.data is undefined);
            // show the loading message instead of flashing it (#665).
            <p className="sub">{t("players.loading")}</p>
          ) : candidates.length === 0 ? (
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

      {can("session:read") && (
        <SessionsView communityId={communityId} serverId={serverId} />
      )}
    </section>
  );
}

function sessionsKey(communityId: string, serverId: string, offset: number) {
  return ["sessions", communityId, serverId, offset] as const;
}

function sessionsUrl(
  communityId: string,
  serverId: string,
  offset: number,
): "/api/communities/{community_id}/servers/{server_id}/sessions" {
  const base = apiPath(
    "/api/communities/{community_id}/servers/{server_id}/sessions",
    { community_id: communityId, server_id: serverId },
  );
  const params = new URLSearchParams({
    limit: String(SESSIONS_PAGE_SIZE),
    offset: String(offset),
  });
  return `${base}?${params.toString()}` as "/api/communities/{community_id}/servers/{server_id}/sessions";
}

function SessionsView({
  communityId,
  serverId,
}: {
  communityId: string;
  serverId: string;
}) {
  const [offset, setOffset] = useState(0);

  const query = useQuery({
    queryKey: sessionsKey(communityId, serverId, offset),
    queryFn: () => api.get(sessionsUrl(communityId, serverId, offset)),
  });

  const sessions: GameSessionResponse[] = query.data?.sessions ?? [];
  const hasPrev = offset > 0;
  const hasNext = sessions.length === SESSIONS_PAGE_SIZE;

  return (
    <div className="card">
      <h2>{t("sessions.heading")}</h2>
      {query.isPending ? (
        <p className="sub">{t("sessions.loading")}</p>
      ) : query.isError ? (
        <p className="field-error">{t("sessions.loadError")}</p>
      ) : sessions.length === 0 && offset === 0 ? (
        <p className="sub">{t("sessions.empty")}</p>
      ) : (
        <>
          <table className="sessions-table">
            <thead>
              <tr>
                <th>{t("sessions.col.hostname")}</th>
                <th>{t("sessions.col.playerIp")}</th>
                <th>{t("sessions.col.username")}</th>
                <th>{t("sessions.col.start")}</th>
                <th>{t("sessions.col.end")}</th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <td>{s.hostname ?? t("sessions.valueUnknown")}</td>
                  <td>{s.player_ip ?? t("sessions.valueUnknown")}</td>
                  <td>{s.username ?? t("sessions.valueUnknown")}</td>
                  <td>
                    {s.started_at !== null
                      ? formatDateTime(s.started_at)
                      : t("sessions.valueUnknown")}
                  </td>
                  <td>
                    {s.ended_at !== null
                      ? formatDateTime(s.ended_at)
                      : t("sessions.active")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {(hasPrev || hasNext) && (
            <div className="pagination">
              <button
                type="button"
                className="btn sm"
                disabled={!hasPrev || query.isFetching}
                onClick={() =>
                  setOffset(Math.max(0, offset - SESSIONS_PAGE_SIZE))
                }
              >
                {t("sessions.prev")}
              </button>
              <button
                type="button"
                className="btn sm"
                disabled={!hasNext || query.isFetching}
                onClick={() => setOffset(offset + SESSIONS_PAGE_SIZE)}
              >
                {t("sessions.next")}
              </button>
            </div>
          )}
        </>
      )}
    </div>
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
