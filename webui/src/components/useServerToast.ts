import { useCallback } from "react";
import { t } from "../i18n/index.ts";
import { type ToastVariant, useToast } from "./Toast.tsx";

/** The server a toast is about. */
export interface ToastServer {
  id: string;
  name: string;
}

/**
 * Raises toasts about a server (WEBUI_SPEC.md 7.4, #3862). The text names the
 * server, since the dashboard acts on several at once, and the server's id
 * scopes the collapse, so the same message about another server, even one of
 * the same name, stays a toast of its own.
 */
export function useServerToast(): (
  server: ToastServer,
  message: string,
  variant: ToastVariant,
) => void {
  const { showToast } = useToast();
  return useCallback(
    (server, message, variant) =>
      showToast(
        t("common.toastAboutServer", { server: server.name, message }),
        variant,
        server.id,
      ),
    [showToast],
  );
}
