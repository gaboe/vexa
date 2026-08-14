/** Isolation harness — Settings config writes. The backend appends its own API path to a stored
 *  endpoint base (`/v1/messages`, `/v1/audio/transcriptions`), so a base pasted from a vendor's
 *  docs (ending in `/v1`) and a bare base MUST persist as the same request target — otherwise the
 *  doubled segment 404s. Asserted on every write path: per-user models, per-user transcription,
 *  and the admin global defaults. */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { setModelPrefs, setTranscriptionPrefs, setGlobalSetting, normalizeBaseUrl } from "../settingsApi";

let fetchMock: ReturnType<typeof vi.fn>;
const lastBody = () => JSON.parse(String((fetchMock.mock.calls.at(-1)![1] as RequestInit).body)) as Record<string, string>;

beforeEach(() => {
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ value: {} }) }) as unknown as Response);
  globalThis.fetch = fetchMock as unknown as typeof fetch;
});
afterEach(() => vi.restoreAllMocks());

describe("settingsApi — endpoint base normalization", () => {
  it("models: a /v1-suffixed base and a bare base persist identically", async () => {
    await setModelPrefs({ base_url: "http://ollama:11434/v1" });
    const suffixed = lastBody();
    await setModelPrefs({ base_url: "http://ollama:11434" });
    expect(suffixed).toEqual(lastBody());
    expect(suffixed.base_url).toBe("http://ollama:11434");
  });

  it("transcription: same for the service URL", async () => {
    await setTranscriptionPrefs({ url: "http://host.docker.internal:3953/v1" });
    const suffixed = lastBody();
    await setTranscriptionPrefs({ url: "http://host.docker.internal:3953" });
    expect(suffixed).toEqual(lastBody());
    expect(suffixed.url).toBe("http://host.docker.internal:3953");
  });

  it("global defaults normalize on the admin route too", async () => {
    await setGlobalSetting("models", { mode: "custom", base_url: "https://openrouter.ai/api/v1/", api_key: "k" });
    expect(lastBody()).toEqual({ mode: "custom", base_url: "https://openrouter.ai/api", api_key: "k" });
    await setGlobalSetting("transcription", { url: "https://transcription.vexa.ai/", token: "t" });
    expect(lastBody()).toEqual({ url: "https://transcription.vexa.ai", token: "t" });
  });

  it("leaves everything else alone (non-/v1 paths, empty = inherit, other keys)", async () => {
    expect(normalizeBaseUrl(" https://gw.example.com/v1beta ")).toBe("https://gw.example.com/v1beta");
    expect(normalizeBaseUrl("https://gw.example.com/openai/v1")).toBe("https://gw.example.com/openai");
    expect(normalizeBaseUrl("")).toBe("");
    await setGlobalSetting("setup", { completed: "true" });
    expect(lastBody()).toEqual({ completed: "true" });
  });
});
