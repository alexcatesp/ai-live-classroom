/**
 * Talks to the local backend.
 *
 * The Tauri shell starts the backend on an ephemeral port and injects the port
 * and the session token into the window before loading the page, so nothing is
 * hardcoded and no second process can use the API by guessing the port.
 */

import type {
  DeviceInventory,
  DiagnosticsReport,
  ListeningStatus,
  Settings,
  SettingsResponse,
  StateSnapshot,
} from "./types";

export interface BackendHandshake {
  port: number;
  token: string;
}

declare global {
  interface Window {
    __AICLASSROOM_BACKEND__?: BackendHandshake;
  }
}

export class BackendError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "BackendError";
  }
}

export function readHandshake(): BackendHandshake | null {
  const injected = typeof window !== "undefined" ? window.__AICLASSROOM_BACKEND__ : undefined;
  if (injected?.port && injected.token) {
    return injected;
  }
  // `npm run dev` against a backend started by hand.
  const port = Number(import.meta.env?.VITE_BACKEND_PORT);
  const token = import.meta.env?.VITE_BACKEND_TOKEN;
  if (port && token) {
    return { port, token };
  }
  return null;
}

export class BackendClient {
  constructor(private readonly handshake: BackendHandshake) {}

  private get base(): string {
    return `http://127.0.0.1:${this.handshake.port}`;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.base}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-AIClassroom-Token": this.handshake.token,
        ...(init.headers ?? {}),
      },
    });

    if (!response.ok) {
      // The backend answers conflicts and validation errors with a `detail`
      // written for the teacher, so it is shown rather than a status code.
      let detail = `Error ${response.status}`;
      try {
        const body = await response.json();
        if (typeof body?.detail === "string") {
          detail = body.detail;
        }
      } catch {
        /* a response with no JSON body keeps the generic message */
      }
      throw new BackendError(detail, response.status);
    }

    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  events(): WebSocket {
    return new WebSocket(
      `ws://127.0.0.1:${this.handshake.port}/ws/events?token=${encodeURIComponent(
        this.handshake.token,
      )}`,
    );
  }

  getState(): Promise<StateSnapshot> {
    return this.request("/api/state");
  }

  prepareClass(): Promise<StateSnapshot> {
    return this.request("/api/class/prepare", { method: "POST" });
  }

  startClass(): Promise<StateSnapshot> {
    return this.request("/api/class/start", { method: "POST" });
  }

  pauseClass(): Promise<StateSnapshot> {
    return this.request("/api/class/pause", { method: "POST" });
  }

  resumeClass(): Promise<StateSnapshot> {
    return this.request("/api/class/resume", { method: "POST" });
  }

  stopClass(): Promise<StateSnapshot> {
    return this.request("/api/class/stop", { method: "POST" });
  }

  recover(): Promise<StateSnapshot> {
    return this.request("/api/class/recover", { method: "POST" });
  }

  getListening(): Promise<ListeningStatus> {
    return this.request("/api/listening");
  }

  getDevices(): Promise<DeviceInventory> {
    return this.request("/api/devices");
  }

  getSettings(): Promise<SettingsResponse> {
    return this.request("/api/settings");
  }

  saveSettings(settings: Settings): Promise<SettingsResponse> {
    return this.request("/api/settings", { method: "PUT", body: JSON.stringify(settings) });
  }

  /** A passphrase makes the stored key portable between computers (R-5). */
  saveApiKey(apiKey: string, passphrase?: string): Promise<void> {
    return this.request("/api/settings/api-key", {
      method: "POST",
      body: JSON.stringify({ api_key: apiKey, passphrase: passphrase || null }),
    });
  }

  unlock(passphrase: string): Promise<SettingsResponse> {
    return this.request("/api/settings/unlock", {
      method: "POST",
      body: JSON.stringify({ passphrase }),
    });
  }

  clearApiKey(): Promise<void> {
    return this.request("/api/settings/api-key", { method: "DELETE" });
  }

  runDiagnostics(): Promise<DiagnosticsReport> {
    return this.request("/api/diagnostics/run", { method: "POST" });
  }

  getLastDiagnostics(): Promise<DiagnosticsReport | null> {
    return this.request("/api/diagnostics/last");
  }
}
