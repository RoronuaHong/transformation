/** FastAPI base URL for workbench calls from the browser. */
export function tryApiBase(): string {
  const env = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "");
  if (env) {
    // Never call :8901 cross-origin from the browser — extensions often block it.
    if (env === "/ops-api" || env.endsWith("/ops-api")) return env;
    if (/^https?:\/\/(127\.0\.0\.1|localhost):8901/.test(env)) return "/ops-api";
    return env;
  }
  return "/ops-api";
}

/** Direct upstream (server-side / CLI only). */
export function tryApiDirect(): string {
  const upstream =
    process.env.VITUAL_API_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8901";
  return upstream;
}
