/** Configuration (spec section 18), including where the API key is entered. */

import { useState } from "react";

import type { DeviceInventory, Settings, SettingsResponse } from "../lib/types";

type LocalServerKey =
  | "local_stt_url"
  | "local_stt_model"
  | "local_llm_url"
  | "local_llm_model"
  | "local_tts_url"
  | "local_tts_voice";

/** The teacher's server (D-14): an address and a model for each service. */
const LOCAL_SERVER_FIELDS: { key: LocalServerKey; label: string; placeholder: string }[] = [
  { key: "local_stt_url", label: "Transcripción (speaches)", placeholder: "http://pc-casa:8000" },
  { key: "local_stt_model", label: "Modelo de transcripción", placeholder: "" },
  {
    key: "local_llm_url",
    label: "Modelo de lenguaje (Ollama)",
    placeholder: "http://pc-casa:11434",
  },
  { key: "local_llm_model", label: "Modelo de Ollama", placeholder: "qwen3:14b" },
  { key: "local_tts_url", label: "Voz (Kokoro)", placeholder: "http://pc-casa:8880" },
  { key: "local_tts_voice", label: "Voz de Kokoro", placeholder: "ef_dora" },
];

interface Props {
  data: SettingsResponse | null;
  devices: DeviceInventory | null;
  saving: boolean;
  error: string | null;
  onSave: (settings: Settings) => void;
  onSaveApiKey: (apiKey: string, passphrase?: string) => void;
  onClearApiKey: () => void;
  onUnlock: (passphrase: string) => void;
}

export function SettingsPanel({
  data,
  devices,
  saving,
  error,
  onSave,
  onSaveApiKey,
  onClearApiKey,
  onUnlock,
}: Props) {
  const [apiKey, setApiKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [portable, setPortable] = useState(false);
  const [unlockPassphrase, setUnlockPassphrase] = useState("");

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

      <fieldset className="field provider">
        <legend>Dónde se responden las preguntas</legend>
        <label className="checkbox">
          <input
            type="radio"
            name="ai-provider"
            checked={settings.ai_provider === "cloud"}
            onChange={() => update({ ai_provider: "cloud" })}
          />
          En la nube (OpenAI)
        </label>
        <label className="checkbox">
          <input
            type="radio"
            name="ai-provider"
            checked={settings.ai_provider === "local"}
            onChange={() => update({ ai_provider: "local" })}
          />
          En mi servidor (faster-whisper, Qwen y Kokoro)
        </label>
        <p className="muted small">
          {settings.ai_provider === "cloud"
            ? "Necesita la clave de la API y se paga por uso."
            : "Sin coste por uso. El PC del servidor debe estar encendido y conectado por " +
              "Tailscale. «Probar conversación» sigue usando OpenAI."}
        </p>
      </fieldset>

      {settings.ai_provider === "local" && (
        <div className="field local-server">
          {LOCAL_SERVER_FIELDS.map(({ key, label, placeholder }) => (
            <div className="field" key={key}>
              <label htmlFor={key}>{label}</label>
              <input
                id={key}
                type="text"
                value={settings[key]}
                placeholder={placeholder}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) => update({ [key]: event.target.value })}
              />
            </div>
          ))}
        </div>
      )}

      {data.requires_passphrase && !data.unlocked && (
        <div className="field unlock" role="alert">
          <label htmlFor="unlock-passphrase">Desbloquea la clave</label>
          <p className="muted">
            La clave guardada está protegida con contraseña. Introdúcela para poder dar clase.
          </p>
          <div className="row">
            <input
              id="unlock-passphrase"
              type="password"
              value={unlockPassphrase}
              autoComplete="off"
              onChange={(event) => setUnlockPassphrase(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && unlockPassphrase.trim()) {
                  onUnlock(unlockPassphrase);
                  setUnlockPassphrase("");
                }
              }}
            />
            <button
              type="button"
              className="primary"
              disabled={unlockPassphrase.trim().length === 0 || saving}
              onClick={() => {
                onUnlock(unlockPassphrase);
                setUnlockPassphrase("");
              }}
            >
              Desbloquear
            </button>
          </div>
        </div>
      )}

      <div className="field">
        <label htmlFor="api-key">Clave de la API</label>
        <p className="muted">
          {!data.api_key_configured
            ? "No hay ninguna clave guardada en este equipo."
            : data.requires_passphrase
              ? "Hay una clave guardada, protegida con contraseña y válida en cualquier equipo."
              : "Hay una clave guardada y cifrada para este equipo."}
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
            disabled={
              apiKey.trim().length === 0 ||
              saving ||
              (portable && passphrase.trim().length === 0)
            }
            onClick={() => {
              onSaveApiKey(apiKey.trim(), portable ? passphrase : undefined);
              setApiKey("");
              setPassphrase("");
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

        <label className="checkbox">
          <input
            type="checkbox"
            checked={portable}
            onChange={(event) => setPortable(event.target.checked)}
          />
          Poder usar esta clave en otros ordenadores
        </label>

        {portable && (
          <div className="row">
            <input
              id="key-passphrase"
              type="password"
              value={passphrase}
              placeholder="Contraseña para la clave"
              autoComplete="off"
              aria-label="Contraseña para la clave"
              onChange={(event) => setPassphrase(event.target.value)}
            />
          </div>
        )}

        <p className="muted small">
          {portable
            ? "La clave se cifrará con esa contraseña y funcionará en cualquier ordenador " +
              "al que copies la carpeta. Tendrás que escribirla al empezar cada sesión, y " +
              "si la olvidas habrá que volver a introducir la clave."
            : "La clave se cifra con la cuenta de Windows de este equipo. Es lo más cómodo, " +
              "pero si copias la carpeta a otro ordenador tendrás que volver a introducirla."}
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
          con ruido. Mide el efecto con scripts/measure_wakeword.py antes de subirla.
        </p>
      </div>

      <div className="field">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={settings.wake_vad_threshold > 0}
            onChange={(event) => update({ wake_vad_threshold: event.target.checked ? 0.5 : 0 })}
          />
          Activar solo cuando haya voz
        </label>
        <p className="muted small">
          Descarta activaciones que no coinciden con una persona hablando: sillas, puertas o el
          ventilador del proyector no pueden despertar al asistente. Déjalo activado salvo que
          estés midiendo.
        </p>
      </div>

      <div className="field">
        <label className="checkbox">
          <input
            type="checkbox"
            checked={settings.echo_guard_margin > 0}
            onChange={(event) => update({ echo_guard_margin: event.target.checked ? 0.15 : 0 })}
          />
          Evitar que el asistente se oiga a sí mismo
        </label>
        <p className="muted small">
          Mientras el asistente habla, exige una activación más clara para no confundir su
          propia voz con una interrupción. Puedes desactivarlo si usas auriculares.
        </p>
      </div>

      {settings.ai_provider === "cloud" && (
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
      )}
    </section>
  );
}
