/**
 * 403 mutation glue (WEBUI_SPEC.md 7.3 / 7.4).
 *
 * Later phases pass any caught error through `onForbidden(error)` in their
 * mutation/error handlers. On a 403 `ApiError` it:
 *   1. toasts the missing permission (named when the body carries the
 *      `permission` extension member, generic otherwise), and
 *   2. re-fetches the active community's capabilities, since a 403 means the
 *      cached set may be stale.
 * Non-403 errors pass through untouched so callers keep their own handling.
 * Given the `server` the action was about, the toast names it, as a lifecycle
 * action's toasts do (WEBUI_SPEC.md 7.4). It is passed as an option, so a bare
 * `onError: onForbidden` cannot take a mutation's variables for the server.
 *
 * Returns whether it handled the error, so a caller can skip its own toast for
 * a 403 it already surfaced here.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { ApiError } from "../api/client.ts";
import { useToast } from "../components/Toast.tsx";
import {
  type ToastServer,
  useServerToast,
} from "../components/useServerToast.ts";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "./ActiveCommunityProvider.tsx";
import { refetchCapabilities } from "./capabilities.ts";

export type OnForbidden = (
  error: unknown,
  options?: { server?: ToastServer },
) => boolean;

export function useOnForbidden(): OnForbidden {
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const showServerToast = useServerToast();
  const { communityId } = useActiveCommunity();

  return useCallback(
    (error: unknown, options?: { server?: ToastServer }): boolean => {
      if (!(error instanceof ApiError) || error.status !== 403) {
        return false;
      }
      const message =
        error.permission !== undefined
          ? t("permissions.deniedNamed", { permission: error.permission })
          : t("permissions.denied");
      if (options?.server === undefined) {
        showToast(message, "error");
      } else {
        showServerToast(options.server, message, "error");
      }
      if (communityId !== null) {
        void refetchCapabilities(queryClient, communityId);
      }
      return true;
    },
    [queryClient, showToast, showServerToast, communityId],
  );
}
