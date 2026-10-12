/** Use the full typed MetaResponse fixture; omitted defensive fields can make tests pass through unintended fallbacks. */

import type { components } from "../api/schema";

type MetaResponse = components["schemas"]["MetaResponse"];

/** Defaults represent relay off and unconfigured memory limits.
 * Bedrock requires relay, so override both flags together. */
export function meta(overrides: Partial<MetaResponse> = {}): MetaResponse {
  return {
    relay_enabled: false,
    bedrock_enabled: false,
    default_memory_limit_mb: null,
    max_memory_limit_mb: null,
    ...overrides,
  };
}
