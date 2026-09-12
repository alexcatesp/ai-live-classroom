import { useCallback, useEffect, useMemo, useState } from "react";

import { ClassControls } from "./components/ClassControls";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { StatePanel } from "./components/StatePanel";
import { WakeWordMeter } from "./components/WakeWordMeter";
import { BackendClient, readHandshake } from "./lib/api";
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
  const client = useMemo(() => (handshake ? new BackendClient(handshake) : null), [handshake]);
  const { snapshot, connected, lastActivation } = useBackendConnection(client);

  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [runningDiagnostics, setRunningDiagnostics] = useState(false);
  const [diagnosticsError, setDiagnosticsError] = useState<string | null>(null);

  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [devices, setDevices] = useState<DeviceInventory | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

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
    async (apiKey: string) => {
      if (!client) return;
      setSaving(true);
      setSettingsError(null);
      try {
        await client.saveApiKey(apiKey);
        setSettings(await client.getSettings());
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
          <h2>No se encontró el motor local</h2>
          <p>
            La aplicación no ha recibido los datos de conexión del proceso local. Cierra la
            aplicación y vuelve a abrirla desde <code>AI-Classroom-Live.exe</code>.
          </p>
        </section>
      </main>
    );
  }

  return (
    <main className="app">
      <header className="app-header">
        <h1>AI Classroom Live</h1>
        <p className="muted">Fase 0 · diagnóstico y palabra de activación</p>
      </header>

      <StatePanel snapshot={snapshot} connected={connected} />

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

      <DiagnosticsPanel
        report={report}
        running={runningDiagnostics}
        error={diagnosticsError}
        onRun={() => void runDiagnostics()}
      />

      <WakeWordMeter status={listening} />

      <SettingsPanel
        data={settings}
        devices={devices}
        saving={saving}
        error={settingsError}
        onSave={(next) => void saveSettings(next)}
        onSaveApiKey={(key) => void saveApiKey(key)}
        onClearApiKey={() => void clearApiKey()}
      />
    </main>
  );
}
