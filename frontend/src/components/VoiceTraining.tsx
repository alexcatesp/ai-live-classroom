/**
 * "Entrenar con mi voz" (D-12): record the phrase and the phrases it gets
 * confused with, then retrain the detector on this machine.
 *
 * The recordings exist only in the backend's memory and are discarded when
 * training ends; the panel says so before anyone presses a button.
 */

import { useCallback, useEffect, useId, useState } from "react";

import type { BackendClient } from "../lib/api";
import type { TakeKind, TakeSummary, VoiceStatus } from "../lib/types";

interface Props {
  client: BackendClient | null;
  /** The class holds the microphone while it listens. */
  classListening: boolean;
}

interface Slot {
  kind: TakeKind;
  slot: number;
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function VoiceTraining({ client, classListening }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [recording, setRecording] = useState<Slot | null>(null);
  const [takeErrors, setTakeErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const contentId = useId();

  const refresh = useCallback(async () => {
    if (!client) return;
    try {
      setStatus(await client.getVoice());
    } catch (failure) {
      setError(describe(failure));
    }
  }, [client]);

  useEffect(() => {
    if (expanded) void refresh();
  }, [expanded, refresh]);

  // Training runs in the backend for a minute or two; follow it while it does.
  const training = status?.state === "training";
  useEffect(() => {
    if (!training) return;
    const timer = setInterval(() => void refresh(), 1000);
    return () => clearInterval(timer);
  }, [training, refresh]);

  const act = async (action: () => Promise<VoiceStatus>) => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await action());
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  };

  const record = async (kind: TakeKind, slot: number) => {
    if (!client) return;
    const key = `${kind}-${slot}`;
    setRecording({ kind, slot });
    setTakeErrors(({ [key]: _gone, ...rest }) => rest);
    setError(null);
    try {
      setStatus(await client.recordTake(kind, slot));
    } catch (failure) {
      setTakeErrors((errors) => ({ ...errors, [key]: describe(failure) }));
    } finally {
      setRecording(null);
    }
  };

  const phraseDone = status?.phrase_takes.filter(Boolean).length ?? 0;
  const nearMissDone = status?.near_miss_takes.filter(Boolean).length ?? 0;
  const allRecorded =
    status !== null &&
    phraseDone === status.phrase_takes.length &&
    nearMissDone === status.near_miss_takes.length;
  const canRecord =
    !classListening && !training && recording === null && status?.state !== "ready";

  const renderTake = (kind: TakeKind, slot: number, label: string, take: TakeSummary | null) => {
    const key = `${kind}-${slot}`;
    const isRecording = recording?.kind === kind && recording.slot === slot;
    return (
      <li key={key} className={`take ${take ? "take-done" : ""}`}>
        <span className="take-label">{label}</span>
        <span className="take-state" aria-live="polite">
          {isRecording ? "Habla ahora…" : take ? "Grabada" : "Sin grabar"}
        </span>
        <button
          type="button"
          onClick={() => void record(kind, slot)}
          disabled={!canRecord}
          aria-label={`${take ? "Repetir" : "Grabar"}: ${label}`}
        >
          {take ? "Repetir" : "Grabar"}
        </button>
        {isRecording && status && (
          <span
            className="take-timer"
            style={{ animationDuration: `${status.take_seconds}s` }}
            aria-hidden="true"
          />
        )}
        {takeErrors[key] && (
          <p className="take-error" role="alert">
            {takeErrors[key]}
          </p>
        )}
      </li>
    );
  };

  return (
    <div className="voice-training">
      <button
        type="button"
        className="accordion-toggle"
        aria-expanded={expanded}
        aria-controls={contentId}
        onClick={() => setExpanded((open) => !open)}
      >
        <svg className="chevron" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
          <path
            d="M5 3l5 5-5 5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Entrenar con mi voz
      </button>

      <div id={contentId} hidden={!expanded} className="voice-content">
        {!status && !error && <p className="muted">Cargando…</p>}
        {error && (
          <p className="warning-line" role="alert">
            {error}
          </p>
        )}

        {status && (
          <>
            {status.personal_model && (
              <div className="personal-model">
                <p>
                  El detector ya usa un modelo entrenado con tu voz.
                </p>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || training}
                  onClick={() => client && void act(() => client.restoreOriginalModel())}
                >
                  Volver al modelo original
                </button>
              </div>
            )}

            {status.unavailable_reason ? (
              <p className="warning-line">{status.unavailable_reason}</p>
            ) : (
              <>
                <p className="muted small">
                  Graba cinco veces «{status.phrase}» y una vez cada frase parecida. Al pulsar
                  «Grabar» tienes {status.take_seconds} segundos: di la frase en seguida y con tu
                  tono normal. Las grabaciones solo existen en la memoria de este equipo, no salen
                  de él y se borran en cuanto termina el entrenamiento.
                </p>

                {classListening && (
                  <p className="warning-line">
                    El micrófono está escuchando la clase. Pausa o finaliza la clase para grabar.
                  </p>
                )}

                <h3>
                  «{status.phrase}» ({phraseDone} de {status.phrase_takes.length})
                </h3>
                <ol className="takes">
                  {status.phrase_takes.map((take, slot) =>
                    renderTake("phrase", slot, `Vez ${slot + 1}`, take),
                  )}
                </ol>

                <h3>
                  Frases parecidas que no deben activarlo ({nearMissDone} de{" "}
                  {status.near_miss_takes.length})
                </h3>
                <ol className="takes">
                  {status.near_miss_takes.map((take, slot) =>
                    renderTake(
                      "near_miss",
                      slot,
                      `«${status.near_miss_prompts[slot] ?? ""}»`,
                      take,
                    ),
                  )}
                </ol>

                {status.state === "idle" && (
                  <button
                    type="button"
                    className="primary"
                    disabled={!allRecorded || busy || recording !== null}
                    onClick={() => client && void act(() => client.trainVoice())}
                  >
                    Entrenar
                  </button>
                )}

                {training && (
                  <div className="training-progress" role="status">
                    <div
                      className="meter"
                      role="progressbar"
                      aria-valuenow={Math.round(status.progress * 100)}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label="Progreso del entrenamiento"
                    >
                      <div className="meter-fill" style={{ width: percent(status.progress) }} />
                    </div>
                    <p className="muted small">
                      {status.message}… Puede tardar un par de minutos. Tus grabaciones ya no se
                      pueden repetir: se borran al terminar.
                    </p>
                  </div>
                )}

                {status.state === "ready" && status.result && (
                  <div className="training-result" role="status">
                    <p className="summary-ok">Modelo nuevo entrenado.</p>
                    <ul>
                      <li>
                        Reconoce {status.result.phrase_detected} de {status.result.phrase_total}{" "}
                        de tus «{status.phrase}».
                      </li>
                      <li>
                        Se activa con {status.result.near_misses_triggered} de{" "}
                        {status.result.near_misses_total} frases parecidas.
                      </li>
                      <li>
                        Con ejemplos que no vio al entrenar: detecta el{" "}
                        {percent(status.result.held_out_detection)} y se equivoca en el{" "}
                        {(status.result.held_out_false_rate * 100).toFixed(2)}%.
                      </li>
                    </ul>
                    <p className="muted small">
                      Tus grabaciones también se usaron para entrenar, así que las dos primeras
                      cifras son optimistas. La prueba de verdad es usarlo en clase. El cambio se
                      aplica la próxima vez que inicies la clase.
                    </p>
                    <div className="controls">
                      <button
                        type="button"
                        className="primary"
                        disabled={busy}
                        onClick={() => client && void act(() => client.acceptVoiceModel())}
                      >
                        Usar este modelo
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy}
                        onClick={() => client && void act(() => client.discardVoiceModel())}
                      >
                        Descartar
                      </button>
                    </div>
                  </div>
                )}

                {status.state === "failed" && (
                  <div className="training-result" role="alert">
                    <p className="summary-failed">{status.error}</p>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => client && void act(() => client.discardVoiceModel())}
                    >
                      Volver a empezar
                    </button>
                  </div>
                )}
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
