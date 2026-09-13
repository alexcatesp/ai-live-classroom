import { afterEach, describe, expect, it, vi } from "vitest";

import { BackendClient, BackendError, readHandshake } from "../api";

const handshake = { port: 54321, token: "token-de-prueba" };

function mockFetch(response: Partial<Response> & { json?: () => Promise<unknown> }) {
  const spy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({}),
    ...response,
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__AICLASSROOM_BACKEND__;
});

describe("readHandshake", () => {
  it("uses the values the shell injected", () => {
    window.__AICLASSROOM_BACKEND__ = handshake;
    expect(readHandshake()).toEqual(handshake);
  });

  it("returns null when the shell injected nothing", () => {
    expect(readHandshake()).toBeNull();
  });

  it("ignores an incomplete injection", () => {
    window.__AICLASSROOM_BACKEND__ = { port: 0, token: "" };
    expect(readHandshake()).toBeNull();
  });
});

describe("BackendClient", () => {
  it("sends the session token on every request", async () => {
    const fetchSpy = mockFetch({ json: async () => ({ state: "IDLE" }) });
    await new BackendClient(handshake).getState();

    expect(fetchSpy).toHaveBeenCalledWith(
      "http://127.0.0.1:54321/api/state",
      expect.objectContaining({
        headers: expect.objectContaining({ "X-AIClassroom-Token": "token-de-prueba" }),
      }),
    );
  });

  it("surfaces the backend's own message rather than a status code", async () => {
    mockFetch({
      ok: false,
      status: 409,
      json: async () => ({ detail: "El evento START_CLASS no es válido en el estado IDLE." }),
    });

    await expect(new BackendClient(handshake).startClass()).rejects.toThrow(
      "no es válido en el estado IDLE",
    );
  });

  it("falls back to a generic message when the error has no body", async () => {
    mockFetch({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error("not json");
      },
    });

    await expect(new BackendClient(handshake).getState()).rejects.toBeInstanceOf(BackendError);
  });

  it("handles the empty body of a 204", async () => {
    mockFetch({ status: 204 });
    await expect(new BackendClient(handshake).saveApiKey("sk-test")).resolves.toBeUndefined();
  });

  it("posts the api key in the body, never in the url", async () => {
    const fetchSpy = mockFetch({ status: 204 });
    await new BackendClient(handshake).saveApiKey("sk-muy-secreta");

    expect(fetchSpy).toHaveBeenCalledWith(
      "http://127.0.0.1:54321/api/settings/api-key",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ api_key: "sk-muy-secreta", passphrase: null }),
      }),
    );
  });

  it("builds the event socket url with the token", () => {
    const sockets: string[] = [];
    vi.stubGlobal(
      "WebSocket",
      class {
        constructor(url: string) {
          sockets.push(url);
        }
      },
    );

    new BackendClient(handshake).events();
    expect(sockets).toEqual(["ws://127.0.0.1:54321/ws/events?token=token-de-prueba"]);
  });
});

describe("readStartupProblem", () => {
  it("passes on what the shell said went wrong", async () => {
    const { readStartupProblem } = await import("../api");
    window.__AICLASSROOM_ERROR__ = "No se encontró el motor local.";
    expect(readStartupProblem()).toBe("No se encontró el motor local.");
    delete window.__AICLASSROOM_ERROR__;
  });

  it("is null when the shell said nothing", async () => {
    const { readStartupProblem } = await import("../api");
    expect(readStartupProblem()).toBeNull();
  });
});
