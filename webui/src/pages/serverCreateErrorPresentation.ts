/**
 * Server-create wizard error presentation contract (WEBUI_SPEC.md 6.3).
 *
 * The new-server and Import ZIP tabs post to sibling routes and share the
 * create reasons, but not their fields: the new-server form has a name and (in
 * relay mode) a slug field, the import form only a name. So a presentation
 * names where the message lands as well as which message it is, and each tab
 * only ever receives a target it can display.
 *
 * A structural `validation_error` on `name` carries the API's own message
 * rather than a translation key, so the component surfaces it before asking
 * this mapper. The page has no 403 glue: `server:create` gates the whole page.
 */

import type { TranslationKey } from "../i18n/index.ts";

export interface CreateErrorPresentation {
  target: "name" | "slug" | "toast";
  key: TranslationKey;
}

export interface ImportErrorPresentation {
  target: "name" | "toast";
  key: TranslationKey;
}

// Create-path problem reasons that map to a specific inline/toast message. A
// 409 `port_taken` is surfaced specifically (issue requirement); everything else
// falls back to the generic toast.
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
  // The config-blob guard (issue #94) as far as this wizard can trip it. The
  // POST carries a flat object of raw override strings plus the two
  // range-checked numbers, so only the size ceiling and the lone-surrogate rule
  // are reachable from here; `config_null_value` and `config_invalid_shape`
  // need a JSON-typed value, which only the Settings tab's editor produces, so
  // arms for those two would be dead.
  config_too_large: "serverCreate.error.config_too_large",
  config_lone_surrogate: "serverCreate.error.config_lone_surrogate",
};

// Mapped reasons answered inline against the field the user typed;
// every other mapped reason is a toast.
const CREATE_ERROR_FIELD: Record<string, "name" | "slug"> = {
  invalid_server_name: "name",
  invalid_slug: "slug",
  slug_taken: "slug",
};

// Import-only reasons, consulted before the shared create reasons.
const IMPORT_ERROR_KEY: Record<string, TranslationKey> = {
  invalid_export_metadata: "serverCreate.import.error.invalid_export_metadata",
  // An archive member stored under the root server.properties path, refused
  // before the row is created (issue #2869): publishing it would stand a
  // directory where the platform keeps a file. Import gets its own string
  // rather than the Backups upload's because the two archives can be tripped
  // by different things — a tar can carry a real server.properties directory
  // member, a zip cannot (`_zip_entries` skips directory entries), so here the
  // offending entry is always a file the operator can find and delete.
  platform_managed_path: "serverCreate.import.error.platform_managed_path",
  // Reachable from import (issue #3022: the join address is auto-assigned, and
  // a racer can take it between the assignment and the commit that inserts the
  // row). It asks for a retry rather than pointing at a field the operator
  // never filled in: the next attempt draws a fresh address.
  slug_taken: "serverCreate.import.error.slug_taken",
};

/** Select where and how the new-server tab shows a failed create. */
export function serverCreateErrorPresentation(
  reason: string | undefined,
): CreateErrorPresentation {
  const key = reason === undefined ? undefined : CREATE_ERROR_KEY[reason];
  if (reason === undefined || key === undefined) {
    return { target: "toast", key: "serverCreate.genericError" };
  }
  return { target: CREATE_ERROR_FIELD[reason] ?? "toast", key };
}

/** Select where and how the Import ZIP tab shows a failed import. */
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
  // The import tab has no slug field, so a reason the create path reports
  // inline against that field is shown as a toast instead. A reason with
  // nowhere to land used to be swallowed, which showed the operator nothing at
  // all (issue #3022). Only `invalid_slug` takes this route today, and import
  // cannot trigger it (it sends no explicit slug).
  const { target, key } = serverCreateErrorPresentation(reason);
  return { target: target === "name" ? "name" : "toast", key };
}
