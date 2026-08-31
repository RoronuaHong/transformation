/** FastAPI base URL for workbench calls from the browser. */
export function tryApiBase(): string {
  const env = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
  if (env) return env;
  // Same-origin proxy avoids browser extensions blocking cross-origin :8900 fetch.
  return "/ops-api";
}

/** Direct upstream for long probes when explicitly configured. */
export function tryApiDirect(): string {
  const env = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
  if (env) return env;
  return "http://127.0.0.1:8900";
}
