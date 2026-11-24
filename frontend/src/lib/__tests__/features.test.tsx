import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import api from "../api";
import { invalidateFeaturesCache, resetFeaturesCache, useFeatures } from "../features";
import { clearSession, getSession, startSession } from "../session";

vi.mock("../api", () => ({ default: { get: vi.fn() } }));

const response = (available: boolean) => ({ data: { registration: true, ai_available: available } });
const get = vi.mocked(api.get);

function deferred() {
  let resolve!: (value: ReturnType<typeof response>) => void;
  const promise = new Promise<ReturnType<typeof response>>((done) => { resolve = done; });
  return { promise, resolve };
}

function signIn(account: "a" | "b") {
  startSession(`test-${account}`, `${account}@example.test`);
}

describe("account capabilities", () => {
  beforeEach(() => {
    clearSession();
    resetFeaturesCache();
    get.mockReset();
  });

  afterEach(() => {
    cleanup();
    clearSession();
  });

  it("loads independent anonymous, A and B states and never reuses the previous result on either switch", async () => {
    const b = deferred();
    const aAgain = deferred();
    get.mockResolvedValueOnce(response(true))
      .mockResolvedValueOnce(response(true))
      .mockResolvedValueOnce(response(false))
      .mockReturnValueOnce(b.promise)
      .mockReturnValueOnce(aAgain.promise);
    const { result } = renderHook(useFeatures);
    await waitFor(() => expect(result.current).toMatchObject({ loaded: true, registration: true, ai_available: false }));

    act(() => signIn("a"));
    await waitFor(() => expect(result.current.ai_available).toBe(true));
    act(clearSession);
    expect(result.current).toMatchObject({ loaded: false, ai_available: false });
    await waitFor(() => expect(result.current.loaded).toBe(true));

    act(() => signIn("b"));
    expect(result.current).toMatchObject({ loaded: false, ai_available: false });
    await act(async () => { b.resolve(response(false)); });
    expect(result.current).toMatchObject({ loaded: true, ai_available: false });

    act(() => signIn("a"));
    expect(result.current).toMatchObject({ loaded: false, ai_available: false });
    await act(async () => { aAgain.resolve(response(true)); });
    expect(result.current).toMatchObject({ loaded: true, ai_available: true });
    expect(get).toHaveBeenCalledTimes(5);
  });

  it("does not publish an old account's response or let its late save invalidate the current account", async () => {
    signIn("a");
    const aSession = getSession();
    const a = deferred();
    get.mockReturnValueOnce(a.promise).mockResolvedValueOnce(response(false));
    const { result } = renderHook(useFeatures);
    act(() => signIn("b"));
    await waitFor(() => expect(result.current).toMatchObject({ loaded: true, ai_available: false }));

    act(() => invalidateFeaturesCache(aSession));
    await act(async () => { a.resolve(response(true)); });
    expect(result.current).toMatchObject({ loaded: true, ai_available: false });
    expect(get).toHaveBeenCalledTimes(2);
    const remounted = renderHook(useFeatures);
    expect(remounted.result.current.ai_available).toBe(false);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("rechecks a changed configuration in the same mounted session and ignores its obsolete request", async () => {
    signIn("a");
    const obsolete = deferred();
    const fresh = deferred();
    get.mockResolvedValueOnce(response(true)).mockReturnValueOnce(obsolete.promise).mockReturnValueOnce(fresh.promise);
    const { result } = renderHook(useFeatures);
    await waitFor(() => expect(result.current.ai_available).toBe(true));

    act(() => invalidateFeaturesCache());
    expect(result.current).toMatchObject({ loaded: false, ai_available: false });
    act(() => invalidateFeaturesCache());
    await act(async () => { obsolete.resolve(response(true)); });
    expect(result.current).toMatchObject({ loaded: false, ai_available: false });
    await act(async () => { fresh.resolve(response(false)); });
    expect(result.current).toMatchObject({ loaded: true, ai_available: false });
    expect(get).toHaveBeenCalledTimes(3);
  });

  it("keeps AI unavailable when the current session's capability request fails", async () => {
    signIn("a");
    get.mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(useFeatures);
    expect(result.current.ai_available).toBe(false);
    await waitFor(() => expect(result.current).toMatchObject({ loaded: true, ai_available: false }));
  });
});
