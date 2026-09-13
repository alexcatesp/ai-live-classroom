import { useCallback, useEffect, useMemo, useState } from "react";

import { ClassControls } from "./components/ClassControls";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { Modal } from "./components/Modal";
import { SettingsPanel } from "./components/SettingsPanel";
import { StatePanel } from "./components/StatePanel";
import { VoiceTraining } from "./components/VoiceTraining";
import { WakeWordMeter } from "./components/WakeWordMeter";
import { BackendClient, readHandshake, readStartupProblem } from "./lib/api";
import { useBackendConnection } from "./lib/useBackend";
import type {
  DeviceInventory,
  DiagnosticsReport,
  ListeningStatus,
  Settings,
  SettingsResponse,
} from "./lib/types";

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function App() {
  const handshake = useMemo(readHandshake, []);
  const startupProblem = useMemo(readStartupProblem, []);
  const client = useMemo(() => (handshake ? new BackendClient(handshake) : null), [handshake]);
  const { snapshot, connected, lastActivation } = useBackendConnection(client);

  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [runningDiagnostics, setRunningDiagnostics] = useState(false);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);

  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [devices, setDevices] = useState<DeviceInventory | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [settingsOpen, setSettingsOpen] = useState(false);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  // A key locked behind a passphrase blocks the class, and settings are now out
  // of sight, so the dialog opens itself rather than wait to be found.
  const locked = Boolean(settings?.requires_passphrase && !settings.unlocked);
  const settingsNeedAttention = locked || settings?.api_key_configured === false;
  useEffect(() => {
    if (locked) setSettingsOpen(true);
  }, [locked]);

  const [listening, setListening] = useState<ListeningStatus | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  // -- initial load -------------------------------------------------------

  useEffect(() => {
    if (!client) return;
    void (async () => {
      try {
        const [loadedSettings, loadedDevices, lastReport] = await Promise.all([
          client.getSettings(),
          client.getDevices(),
          client.getLastDiagnostics(),
        ]);
        setSettings(loadedSettings);
        setDevices(loadedDevices);
        setReport(lastReport);
      } catch (error) {
        setSettingsError(describe(error));
      }
    })();
  }, [client]);

  // -- the detector meter, polled only while the class is listening -------

  const isListeningState =
    snapshot?.state === "PASSIVE_LISTENING" ||
    snapshot?.state === "ACTIVATED" ||
    snapshot?.state === "CAPTURING_REQUEST";

  useEffect(() => {
    if (!client || !isListeningState) {
      setListening(null);
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const status = await client.getListening();
        if (!cancelled) setListening(status);
      } catch {
        /* a momentary failure just leaves the previous reading on screen */
      }
    };
    void poll();
    const timer = setInterval(poll, 500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, isListeningState, lastActivation]);

  // -- actions ------------------------------------------------------------

  const runDiagnostics = useCallback(async () => {
    if (!client) return;
    setRunningDiagnostics(true);
    setDiagnosticsError(null);
    try {
      setReport(await client.runDiagnostics());
    } catch (error) {
      setDiagnosticsError(describe(error));
    } finally {
      setRunningDiagnostics(false);
    }
  }, [client]);

  const control = useCallback(
    (action: (client: BackendClient) => Promise<unknown>) => async () => {
      if (!client) return;
      setControlBusy(true);
      setControlError(null);
      try {
        await action(client);
      } catch (error) {
        setControlError(describe(error));
      } finally {
        setControlBusy(false);
      }
    },
    [client],
  );

  const saveSettings = useCallback(
    async (next: Settings) => {
      if (!client) return;
      setSaving(true);
      setSettingsError(null);
      try {
        setSettings(await client.saveSettings(next));
      } catch (error) {
        setSettingsError(describe(error));
      } finally {
        setSaving(false);
      }
    },
    [client],
  );

  const saveApiKey = useCallback(
    async (apiKey: string, passphrase?: string) => {
      if (!client) return;
      setSaving(true);
      setSettingsError(null);
      try {
        await client.saveApiKey(apiKey, passphrase);
        setSettings(await client.getSettings());
      } catch (error) {
        setSettingsError(describe(error));
      } finally {
        setSaving(false);
      }
    },
    [client],
  );

  const unlock = useCallback(
    async (passphrase: string) => {
      if (!client) return;
      setSaving(true);
      setSettingsError(null);
      try {
        setSettings(await client.unlock(passphrase));
      } catch (error) {
        setSettingsError(describe(error));
      } finally {
        setSaving(false);
      }
    },
    [client],
  );

  const clearApiKey = useCallback(async () => {
    if (!client) return;
    setSaving(true);
    try {
      await client.clearApiKey();
      setSettings(await client.getSettings());
    } catch (error) {
      setSettingsError(describe(error));
    } finally {
      setSaving(false);
    }
  }, [client]);

  if (!handshake) {
    return (
      <main className="app">
        <h1>AI Classroom Live</h1>
        <section className="panel" role="alert">
          <h2>No se pudo arrancar el motor local</h2>
          {startupProblem ? (
            <p className="startup-problem">{startupProblem}</p>
          ) : (
            <p>
              La aplicación no ha recibido los datos de conexión del proceso local.
            </p>
          )}
          <p className="muted">Comprueba, en este orden:</p>
          <ol className="muted">
            <li>
              Que junto a <code>AI-Classroom-Live.exe</code> está la carpeta{" "}
              <code>runtime\backend\</code> con todo su contenido. Si descomprimiste
              solo algunos archivos, vuelve a hacerlo entero.
            </li>
            <li>
              Que el antivirus del centro no se ha llevado{" "}
              <code>runtime\backend\aiclassroom-backend.exe</code>. Es el caso más
              frecuente, y está explicado en <code>docs/antivirus.md</code>.
            </li>
            <li>
              El detalle completo en <code>data\arranque.log</code>, junto a la
              aplicación.
            </li>
          </ol>
        </section>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="app-header">
        <div>
          <h1>AI Classroom Live</h1>
          <p className="muted">Fase 0 · diagnóstico y palabra de activación</p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Configuración"
          aria-haspopup="dialog"
          onClick={() => setSettingsOpen(true)}
        >
          <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
            <path
              fill="currentColor"
              d="M19.14 12.94a7.5 7.5 0 0 0 .05-.94 7.5 7.5 0 0 0-.05-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.3 7.3 0 0 0-1.63-.94l-.36-2.54a.5.5 0 0 0-.5-.42h-3.84a.5.5 0 0 0-.5.42l-.36 2.54c-.59.24-1.13.56-1.63.94l-2.39-.96a.5.5 0 0 0-.61.22L2.63 8.84a.5.5 0 0 0 .12.64l2.03 1.58a7.5 7.5 0 0 0 0 1.88l-2.03 1.58a.5.5 0 0 0-.12.64l1.92 3.32c.13.22.39.3.61.22l2.39-.96c.5.38 1.04.7 1.63.94l.36 2.54c.05.24.26.42.5.42h3.84c.24 0 .45-.18.5-.42l.36-2.54c.59-.24 1.13-.56 1.63-.94l2.39.96c.22.08.48 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64zM12 15.5A3.5 3.5 0 1 1 12 8.5a3.5 3.5 0 0 1 0 7z"
            />
          </svg>
          {settingsNeedAttention && <span className="attention-dot" aria-hidden="true" />}
        </button>
      </header>

      {/* In the order a class happens: check the equipment, run the class,
          watch the detector. */}
      <DiagnosticsPanel
        report={report}
        running={runningDiagnostics}
        error={diagnosticsError}
        onRun={() => void runDiagnostics()}
      />

      <StatePanel snapshot={snapshot} connected={connected}>
        {controlError && (
          <p className="warning-line" role="alert">
            {controlError}
          </p>
        )}
        <ClassControls
          state={snapshot?.state ?? null}
          busy={controlBusy}
          readyToStart={report?.ready_to_start ?? false}
          onPrepare={control((backend) => backend.prepareClass())}
          onStart={control((backend) => backend.startClass())}
          onPause={control((backend) => backend.pauseClass())}
          onResume={control((backend) => backend.resumeClass())}
          onStop={control((backend) => backend.stopClass())}
          onRecover={control((backend) => backend.recover())}
        />
      </StatePanel>

      <WakeWordMeter status={listening}>
        <VoiceTraining client={client} classListening={Boolean(snapshot?.microphone_active)} />
      </WakeWordMeter>

      <Modal title="Configuración" open={settingsOpen} onClose={closeSettings}>
        <SettingsPanel
          data={settings}
          devices={devices}
          saving={saving}
          error={settingsError}
          onSave={(next) => void saveSettings(next)}
          onSaveApiKey={(key, passphrase) => void saveApiKey(key, passphrase)}
          onClearApiKey={() => void clearApiKey()}
          onUnlock={(passphrase) => void unlock(passphrase)}
        />
      </Modal>
    </main>
  );
}
