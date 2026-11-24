import { useSyncExternalStore } from "react";
import type { NavigateFunction } from "react-router-dom";

const readToken = () => localStorage.getItem("token");
let session = { token: readToken(), revision: 0 };
const listeners = new Set<() => void>();

function announceSessionChanged() {
  session = { token: readToken(), revision: session.revision + 1 };
  listeners.forEach((listener) => listener());
}

window.addEventListener("storage", (event) => {
  if (event.storageArea === localStorage && (event.key === "token" || event.key === null)) {
    announceSessionChanged();
  }
});

export function subscribeSession(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function getSession() {
  return session;
}

export function isCurrentSession(expected: ReturnType<typeof getSession>) {
  return expected === session && expected.token === readToken();
}

export function useSession() {
  return useSyncExternalStore(subscribeSession, getSession);
}

export function startSession(token: string, email: string) {
  localStorage.setItem("email", email);
  localStorage.setItem("token", token);
  announceSessionChanged();
}

export function clearSession() {
  localStorage.removeItem("email");
  localStorage.removeItem("token");
  announceSessionChanged();
}

export function currentEmail() {
  return localStorage.getItem("email") || "";
}

export function logout(navigate: NavigateFunction) {
  clearSession();
  navigate("/login");
}

/** Broadcast that the paper list changed (import, delete, retry) so open views refresh. */
export const TASKS_CHANGED = "easypaper:tasks-changed";
export function announceTasksChanged() {
  window.dispatchEvent(new Event(TASKS_CHANGED));
}

/** Broadcast that review counts changed so the navigation badge refreshes. */
export const REVIEW_CHANGED = "easypaper:review-changed";
export function announceReviewChanged() {
  window.dispatchEvent(new Event(REVIEW_CHANGED));
}
