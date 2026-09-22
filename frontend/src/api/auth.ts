import { apiFetch } from "./client";
import type { CurrentUser } from "./types";

export function login(username: string, password: string): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/auth/login", {
    method: "POST",
    body: { username, password },
    skipAuthReset: true,
  });
}

export function logout(): Promise<{ status: string }> {
  return apiFetch<{ status: string }>("/auth/logout", { method: "POST" });
}

export function me(): Promise<CurrentUser> {
  return apiFetch<CurrentUser>("/auth/me", { skipAuthReset: true });
}
