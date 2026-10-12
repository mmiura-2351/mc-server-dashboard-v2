/** Read the tail of logs/forge-install.log on demand through the files API.
 * A crashed server with running intent must be stopped before that API permits the read. */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { type TranslationKey, t } from "../i18n/index.ts";
import { decodeBase64Text } from "./fileText.ts";

type FileContent = components["schemas"]["FileContentResponse"];

const INSTALL_LOG_PATH = "logs/forge-install.log";

export const INSTALL_LOG_TAIL_LINES = 200;

export function tailLines(
  text: string,
  max: number,
): { text: string; truncated: boolean } {
  const lines = text.split("\n");
  if (lines.at(-1) === "") {
    lines.pop();
  }
  return {
    text: lines.slice(-max).join("\n"),
    truncated: lines.length > max,
  };
}

function loadErrorKey(error: unknown): TranslationKey {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return "serverDetail.installLog.missing";
    }
    if (error.status === 409 && error.reason === "server_unsettled") {
      return "serverDetail.installLog.unsettled";
    }
    if (error.status === 413) {
      return "serverDetail.installLog.tooLarge";
    }
  }
  return "serverDetail.installLog.error";
}

export function ForgeInstallLog({
  communityId,
  serverId,
}: {
  communityId: string;
  serverId: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        className="link"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {t(
          open
            ? "serverDetail.installLog.hide"
            : "serverDetail.installLog.view",
        )}
      </button>
      {open && (
        <InstallLogPanel communityId={communityId} serverId={serverId} />
      )}
    </div>
  );
}

function InstallLogPanel({
  communityId,
  serverId,
}: {
  communityId: string;
  serverId: string;
}) {
  const query = useQuery({
    queryKey: ["files", "content", communityId, serverId, INSTALL_LOG_PATH],
    queryFn: ({ signal }) =>
      api.get(
        `${apiPath(
          "/api/communities/{community_id}/servers/{server_id}/files",
          { community_id: communityId, server_id: serverId },
        )}?path=${encodeURIComponent(INSTALL_LOG_PATH)}` as never,
        { signal },
      ) as Promise<FileContent>,
    // These refusals require a state or file change; an immediate retry cannot resolve them.
    retry: false,
  });

  if (query.isPending) {
    return <div>{t("serverDetail.installLog.loading")}</div>;
  }
  if (query.isError) {
    return <div>{t(loadErrorKey(query.error))}</div>;
  }
  const tail = tailLines(
    decodeBase64Text(query.data.content_base64, {
      path: INSTALL_LOG_PATH,
      mcVersion: null,
    }).text,
    INSTALL_LOG_TAIL_LINES,
  );
  return (
    <>
      {tail.truncated && (
        <div>
          {t("serverDetail.installLog.truncated", {
            count: INSTALL_LOG_TAIL_LINES,
          })}
        </div>
      )}
      <pre
        className="log-view install-log-view"
        role="log"
        aria-label={t("serverDetail.installLog.label")}
      >
        {tail.text}
      </pre>
    </>
  );
}
