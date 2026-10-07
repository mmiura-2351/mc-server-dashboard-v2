/**
 * The "View install log" affordance of the crash banner (issue #1093).
 *
 * When a Forge server crashed in its supervised install phase, the installer's
 * output is in `logs/forge-install.log` in the working set. This reads it
 * through the ordinary files API — no dedicated endpoint, no streaming — only
 * once the operator opens it, and shows the tail: the log is megabytes of
 * download and patch lines, appended to on every attempt, and the failure is
 * at the end.
 *
 * The files API serves a file only for a server at rest or running (SPEC
 * 6.9). A server that crashed under a running intent is neither, so the read
 * is refused 409 `server_unsettled` until the operator stops it; that, a log
 * that was never written (404) and one past the read cap (413) each get their
 * own line instead of an error.
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client.ts";
import { apiPath } from "../api/path.ts";
import type { components } from "../api/schema";
import { type TranslationKey, t } from "../i18n/index.ts";
import { decodeBase64Text } from "./fileText.ts";

type FileContent = components["schemas"]["FileContentResponse"];

/** Where the Worker writes the installer's output (worker `ForgeInstallLogRelpath`). */
const INSTALL_LOG_PATH = "logs/forge-install.log";

/** How many trailing lines of the install log are shown. */
export const INSTALL_LOG_TAIL_LINES = 200;

/** The last `max` lines of `text`, and whether earlier ones were cut. */
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
    // None of the refusals above changes on a retry.
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
