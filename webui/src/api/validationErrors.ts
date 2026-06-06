/**
 * Map a structural FastAPI/Pydantic 422 (problem `reason` "validation_error")
 * to inline per-field messages.
 *
 * Such a body carries an `errors` list (AUTH_API.md 2; entry shape `loc`/`msg`
 * after the #393/#395 scrub of `input`/`ctx`). Each entry's field is the tail of
 * its `loc` (`["body", "<field>"]`); the validator `msg` is used verbatim. The
 * caller supplies the set of form fields it knows how to surface, so an entry
 * for an unknown field is ignored. Returns the inline errors keyed by field, or
 * null when no entry maps to a known field — the caller then falls back to its
 * generic toast.
 */

interface ValidationEntry {
  loc: unknown[];
  msg: string;
}

export function fieldErrorsFromValidation<Field extends string>(
  body: unknown,
  fields: readonly Field[],
): Partial<Record<Field, string>> | null {
  if (typeof body !== "object" || body === null || !("errors" in body)) {
    return null;
  }
  const { errors } = body as { errors: unknown };
  if (!Array.isArray(errors)) {
    return null;
  }
  const known = new Set<string>(fields);
  const mapped: Partial<Record<Field, string>> = {};
  for (const entry of errors as ValidationEntry[]) {
    const field = entry.loc?.[entry.loc.length - 1];
    if (
      typeof field === "string" &&
      known.has(field) &&
      typeof entry.msg === "string"
    ) {
      const key = field as Field;
      if (mapped[key] === undefined) {
        mapped[key] = entry.msg;
      }
    }
  }
  return Object.keys(mapped).length > 0 ? mapped : null;
}
