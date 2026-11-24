import { useEffect, useSyncExternalStore } from "react";
import api from "./api";
import { getSession, isCurrentSession, subscribeSession } from "./session";

export type Features = {
  registration: boolean;
  local_sync: boolean;
  ai_available: boolean;
  max_upload_mb: number;
  max_pages: number;
  gloss_preparation_site: "server" | "remote_worker" | "user_choice";
  gloss_worker_configured: boolean;
  /** False while capabilities for the current session are unconfirmed. */
  loaded: boolean;
};

const FALLBACK: Features = {
  registration: false,
  local_sync: false,
  ai_available: false,
  max_upload_mb: 15,
  max_pages: 150,
  gloss_preparation_site: "server",
  gloss_worker_configured: false,
  loaded: false,
};

const UNAVAILABLE: Features = { ...FALLBACK, loaded: true };
let snapshot = { generation: 0, value: FALLBACK };
let loading = false;
const listeners = new Set<() => void>();
const getSnapshot = () => snapshot;

function subscribeFeatures(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function publish(next: typeof snapshot) {
  snapshot = next;
  listeners.forEach((listener) => listener());
}

export function invalidateFeaturesCache(session = getSession()) {
  if (!isCurrentSession(session)) return;
  loading = false;
  publish({ generation: snapshot.generation + 1, value: FALLBACK });
}

subscribeSession(invalidateFeaturesCache);

function loadFeatures(generation: number) {
  const session = getSession();
  if (generation !== snapshot.generation || !isCurrentSession(session) || loading) return;
  if (snapshot.value.loaded && snapshot.value !== UNAVAILABLE) return;
  loading = true;

  const settle = (value: Features) => {
    if (generation !== snapshot.generation || !isCurrentSession(session)) return;
    loading = false;
    publish({ generation, value });
  };

  void api.get<Partial<Features>>("/api/auth/features").then(
    ({ data }) => settle({
      ...FALLBACK,
      ...data,
      ai_available: Boolean(session.token) && data?.ai_available === true,
      loaded: true,
    }),
    () => settle(UNAVAILABLE),
  );
}

export function useFeatures(): Features {
  const { generation, value } = useSyncExternalStore(subscribeFeatures, getSnapshot);
  useEffect(() => { loadFeatures(generation); }, [generation]);
  return value;
}

/** Test hook: forget the cached capabilities. */
export function resetFeaturesCache() {
  invalidateFeaturesCache();
}
