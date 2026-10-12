/** On 403, show the missing permission and invalidate capabilities; return whether the error was handled. */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { ApiError } from "../api/client.ts";
import { useToast } from "../components/Toast.tsx";
import { t } from "../i18n/index.ts";
import { useActiveCommunity } from "./ActiveCommunityProvider.tsx";
import { refetchCapabilities } from "./capabilities.ts";

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
      const message =
        error.permission !== undefined
          ? t("permissions.deniedNamed", { permission: error.permission })
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
