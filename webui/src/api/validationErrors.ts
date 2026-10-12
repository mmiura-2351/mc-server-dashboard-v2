/** Map known fields from validation_error entries using each loc tail and msg.
 * Return null when no field matches so the caller can use its generic error. */

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
