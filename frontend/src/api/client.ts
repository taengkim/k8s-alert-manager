const API_BASE = "/api/v1";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const UNAUTHORIZED_EVENT = "kam:unauthorized";

/**
 * Subscribe to global 401 notifications. Returns an unsubscribe function.
 */
export function onUnauthorized(listener: () => void): () => void {
  const handler = () => listener();
  window.addEventListener(UNAUTHORIZED_EVENT, handler);
  return () => window.removeEventListener(UNAUTHORIZED_EVENT, handler);
}

function notifyUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

function extractDetail(data: unknown, fallback: string): string {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      // FastAPI/Pydantic validation errors: a list of {loc, msg, type}.
      return detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : JSON.stringify(item),
        )
        .join("; ");
    }
    if (detail !== undefined) {
      return JSON.stringify(detail);
    }
  }
  return fallback;
}

interface ApiFetchInit {
  method?: string;
  body?: unknown;
  /** Skip the global 401 -> logged-out reset (used by /auth/me and /auth/login). */
  skipAuthReset?: boolean;
}

export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const { method = "GET", body, skipAuthReset = false } = init;

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    let data: unknown;
    try {
      data = await res.json();
    } catch {
      // non-JSON error body; fall back to statusText below
    }
    const detail = extractDetail(data, res.statusText || `request failed (${res.status})`);

    if (res.status === 401 && !skipAuthReset) {
      notifyUnauthorized();
    }

    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) {
    return undefined as T;
  }

  return (await res.json()) as T;
}
