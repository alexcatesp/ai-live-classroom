/** Configuration (spec section 18), including where the API key is entered. */

import { useState } from "react";

import type { DeviceInventory, Settings, SettingsResponse } from "../lib/types";

interface Props {
  data: SettingsResponse | null;
  devices: DeviceInventory | null;
  saving: boolean;
  error: string | null;
  onSave: (settings: Settings) => void;
  onSaveApiKey: (apiKey: string) => void;
  onClearApiKey: () => void;
}

export function SettingsPanel({
  data,
  devices,
  saving,
  error,
  onSave,
  onSaveApiKey,
  onClearApiKey,
}: Props) {
  const [apiKey, setApiKey] = useState("");

  if (!data) {
    return (
      <section className="panel" aria-label="Configuración">
        <h2>Configuración</h2>
        <p className="muted">Cargando…</p>
      </section>
    );
  }

  const settings = data.settings;
  const update = (patch: Partial<Settings>) => onSave({ ...settings, ...patch });

  return (
    <section className="panel" aria-label="Configuración">
      <h2>Configuración</h2>

      {error && (
        <p className="warning-line" role="alert">
          {error}
        </p>
      )}

      <div className="field">
        <label htmlFor="api-key">Clave de la API</label>
        <p className="muted">
          {data.api_key_configured
            ? "Hay una clave guardada y cifrada en este equipo."
            : "No hay ninguna clave guardada en este equipo."}
        </p>
        <div className="row">
          <input
            id="api-key"
            type="password"
            value={apiKey}
            placeholder="sk-…"
            autoComplete="off"
            onChange={(event) => setApiKey(event.target.value)}
          />
          <button
            type="button"
            disabled={apiKey.trim().length === 0 || saving}
            onClick={() => {
              onSaveApiKey(apiKey.trim());
              setApiKey("");
            }}
          >
            Guardar clave
          </button>
          {data.api_key_configured && (
            <button type="button" className="secondary" onClick={onClearApiKey} disabled={saving}>
              Borrar
            </button>
          )}
        </div>
        <p className="muted small">
          La clave se cifra con la cuenta de Windows de este equipo. Si copias la carpeta a otro
          ordenador, tendrás que volver a introducirla.
        </p>
      </div>

      <div className="field">
        <label htmlFor="input-device">Micrófono</label>
        <select
          id="input-device"
          value={settings.input_device ?? ""}
          onChange={(event) => update({ input_device: event.target.value || null })}
        >
          <option value="">Predeterminado del sistema</option>
          {devices?.inputs.map((device) => (
            <option key={device.index} value={device.name}>
              {device.name}
            </option>
          ))}
        </select>
      </div>

      <div className="field">
        <label htmlFor="output-device">Altavoces</label>
        <select
          id="output-device"
          value={settings.output_device ?? ""}
          onChange={(event) => update({ output_device: event.target.value || null })}
        >
          <option value="">Predeterminado del sistema</option>
          {devices?.outputs.map((device) => (
            <option key={device.index} value={device.name}>
              {device.name}
            </option>
          ))}
        </select>
      </div>

      {devices?.error && <p className="warning-line">{devices.error}</p>}

      <div className="field">
        <label htmlFor="sensitivity">
          Sensibilidad del detector ({settings.wake_sensitivity.toFixed(2)})
        </label>
        <input
          id="sensitivity"
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={settings.wake_sensitivity}
          onChange={(event) => update({ wake_sensitivity: Number(event.target.value) })}
        />
        <p className="muted small">
          Más sensibilidad detecta mejor la frase, pero aumenta los falsos positivos en un aula
          con ruido.
        </p>
      </div>

      <div className="field">
        <label htmlFor="model">Modelo de voz</label>
        <input
          id="model"
          type="text"
          value={settings.realtime_model}
          onChange={(event) => update({ realtime_model: event.target.value })}
        />
        <p className="muted small">
          Comprueba en la cuenta qué modelo está autorizado antes de la clase.
        </p>
      </div>
    </section>
  );
}
