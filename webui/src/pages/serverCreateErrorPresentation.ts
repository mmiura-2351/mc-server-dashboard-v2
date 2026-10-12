/** Create can target name, slug, or toast; import has no slug field and must use name or toast.
 * Structural name errors carry API messages and are handled before this mapper. */

import type { TranslationKey } from "../i18n/index.ts";

export interface CreateErrorPresentation {
  target: "name" | "slug" | "toast";
  key: TranslationKey;
}

export interface ImportErrorPresentation {
  target: "name" | "toast";
  key: TranslationKey;
}

const CREATE_ERROR_KEY: Record<string, TranslationKey> = {
  port_taken: "serverCreate.error.port_taken",
  port_out_of_range: "serverCreate.error.port_out_of_range",
  server_name_exists: "serverCreate.error.server_name_exists",
  invalid_server_name: "serverCreate.error.invalid_server_name",
  unknown_version: "serverCreate.error.unknown_version",
  invalid_memory_limit: "serverCreate.error.invalid_memory_limit",
  invalid_cpu_allocation: "serverCreate.error.invalid_cpu_allocation",
  invalid_slug: "serverCreate.error.invalid_slug",
  slug_taken: "serverCreate.error.slug_taken",
  // Flat strings and validated numeric overrides can trigger only size and lone-surrogate config guards.
  config_too_large: "serverCreate.error.config_too_large",
  config_lone_surrogate: "serverCreate.error.config_lone_surrogate",
};

const CREATE_ERROR_FIELD: Record<string, "name" | "slug"> = {
  invalid_server_name: "name",
  invalid_slug: "slug",
  slug_taken: "slug",
};

const IMPORT_ERROR_KEY: Record<string, TranslationKey> = {
  invalid_export_metadata: "serverCreate.import.error.invalid_export_metadata",
  // ZIP directory entries are skipped, so this refusal names a file below server.properties/.
  platform_managed_path: "serverCreate.import.error.platform_managed_path",
  // Import auto-allocates its slug; a commit-time collision needs a retry, not a slug-field correction.
  slug_taken: "serverCreate.import.error.slug_taken",
};

export function serverCreateErrorPresentation(
  reason: string | undefined,
): CreateErrorPresentation {
  const key = reason === undefined ? undefined : CREATE_ERROR_KEY[reason];
  if (reason === undefined || key === undefined) {
    return { target: "toast", key: "serverCreate.genericError" };
  }
  return { target: CREATE_ERROR_FIELD[reason] ?? "toast", key };
}

export function serverImportErrorPresentation(
  status: number | undefined,
  reason: string | undefined,
): ImportErrorPresentation {
  if (status === 413) {
    return { target: "toast", key: "serverCreate.import.tooLarge" };
  }
  const importKey = reason === undefined ? undefined : IMPORT_ERROR_KEY[reason];
  if (importKey !== undefined) {
    return { target: "toast", key: importKey };
  }
  // Import has no slug field; show those shared create errors as toasts instead.
  const { target, key } = serverCreateErrorPresentation(reason);
  return { target: target === "name" ? "name" : "toast", key };
}
