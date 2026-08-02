/**
 * Shared fixture for the `GET /api/meta` body (issue #2537).
 *
 * Promoted out of `ServerDetailPage.test.tsx` (PR #2536, issue #2525) so the
 * four test files that stub this endpoint answer it with one truthful shape
 * instead of four hand-written subsets. A partial stub is not a harmless
 * shortcut here: every consumer reads these fields defensively
 * (`typeof x === "number"`, `x === true`), so an omitted field is
 * indistinguishable from a legitimately absent one and a test can pass for the
 * wrong reason.
 *
 * Typed against the generated `MetaResponse`, so a contract change reddens this
 * fixture at type-check time rather than silently drifting from the API.
 */

import type { components } from "../api/schema";

type MetaResponse = components["schemas"]["MetaResponse"];

/**
 * A full `MetaResponse`, overridable field by field.
 *
 * Defaults mirror a relay-off deployment with no operator memory knobs set:
 * `null` means "the operator has not configured this", which is what the API
 * really sends (`api/src/mc_server_dashboard_api/core/api/meta.py`).
 *
 * Note the API-side invariant `bedrock_enabled = relay_enabled AND
 * relay.bedrock_enabled`: Bedrock on with the relay off is not a state any
 * deployment can report, so override both together.
 */
export function meta(overrides: Partial<MetaResponse> = {}): MetaResponse {
  return {
    relay_enabled: false,
    bedrock_enabled: false,
    default_memory_limit_mb: null,
    max_memory_limit_mb: null,
    ...overrides,
  };
}
