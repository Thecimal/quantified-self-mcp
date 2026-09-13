/**
 * Stable, machine-parseable codes prefixed onto every tool error message
 * (as "[code] human message"), mirroring the Python original — a client
 * or calling LLM can branch on the failure kind (e.g. retry on
 * "database_locked" but not "invalid_date") without parsing free-form
 * English.
 */
export const ERR_INVALID_DATE = "invalid_date";
export const ERR_INVALID_RANGE = "invalid_range";
export const ERR_MISSING_METRIC = "missing_metric";
export const ERR_INVALID_METRIC_VALUE = "invalid_metric_value";
export const ERR_INVALID_FIELD = "invalid_field";
export const ERR_DATABASE_LOCKED = "database_locked";
export const ERR_DATABASE_ERROR = "database_error";
export const ERR_INVALID_TIMESTAMP = "invalid_timestamp";
export const ERR_INVALID_METRIC = "invalid_metric";

/**
 * Thrown by tool logic for errors the calling model can act on. Always
 * delivered to the client in full (as an MCP tool error result) — unlike
 * an unexpected internal error, which is reduced to a generic message so
 * a corrupt DB or disk issue doesn't leak a raw stack trace / local file
 * paths into whatever LLM is calling this tool.
 */
export class ToolError extends Error {
  constructor(code: string, message: string) {
    super(`[${code}] ${message}`);
    this.name = "ToolError";
  }
}

export function toolError(code: string, message: string): ToolError {
  return new ToolError(code, message);
}

/**
 * True if exc looks like a lock/busy contention error rather than a
 * missing/corrupt database — used to pick database_locked vs
 * database_error so the two failure modes (retry-worthy vs not) are
 * distinguishable by code, not just message text.
 */
export function isLockedError(exc: unknown): boolean {
  const msg = exc instanceof Error ? exc.message.toLowerCase() : String(exc).toLowerCase();
  return msg.includes("lock") || msg.includes("busy");
}
