/**
 * 403 mutation glue (WEBUI_SPEC.md 7.3 / 7.4).
 *
 * Later phases pass any caught error through `onForbidden(error)` in their
 * mutation/error handlers. On a 403 `ApiError` it:
 *   1. toasts the missing permission (named when the `reason` is a permission
 *      code, generic otherwise), and
 *   2. re-fetches the active community's capabilities, since a 403 means the
 *      cached set may be stale.
 * Non-403 errors pass through untouched so callers keep their own handling.
 *
 * Returns whether it handled the error, so a caller can skip its own toast for
 * a 403 it already surfaced here.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { ApiError } from "../api/client.ts";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "./ActiveCommunityProvider.tsx";
import { refetchCapabilities } from "./capabilities.ts";

/** A permission code looks like `family:action` (WEBUI_SPEC.md 2.2). */
function isPermissionCode(reason: string | undefined): reason is string {
  return reason !== undefined && /^[a-z]+:[a-z]+$/.test(reason);
}

export type OnForbidden = (error: unknown) => boolean;

export function useOnForbidden(): OnForbidden {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { communityId } = useActiveCommunity();

  return useCallback(
    (error: unknown): boolean => {
      if (!(error instanceof ApiError) || error.status !== 403) {
        return false;
      }
      const message = isPermissionCode(error.reason)
        ? t("permissions.deniedNamed") + error.reason
        : t("permissions.denied");
      showToast(message, "error");
      if (communityId !== null) {
        void refetchCapabilities(queryClient, communityId);
      }
      return true;
    },
    [queryClient, showToast, communityId],
  );
}
