/**
 * Live view of the detector (risk R-1).
 *
 * The point of this panel is measurement: it shows the scores against the
 * threshold so the sensitivity can be tuned in a real classroom rather than
 * guessed at a desk.
 */

import type { ReactNode } from "react";

import type { ListeningStatus } from "../lib/types";

/** Matches DEFAULT_VAD_THRESHOLD in backend/src/aiclassroom/audio/wakeword.py. */
const SPEECH_THRESHOLD = 0.5;
/** About -57 dBFS: below this a real microphone is not delivering anything. */
const SILENT_LEVEL = 0.05;

interface Props {
  status: ListeningStatus | null;
  /** Shown below the meter whether or not the class is listening. */
  children?: ReactNode;
}

export function WakeWordMeter({ status, children }: Props) {
  if (!status || !status.listening) {
    return (
      <section className="panel" aria-label="Palabra de activación">
        <h2>Palabra de activación</h2>
        <p className="muted">Disponible cuando la clase esté en escucha pasiva.</p>
        {children}
      </section>
    );
  }

  const threshold = status.threshold ?? 0;
  const scores = status.recent_scores;
  const latest = scores.length > 0 ? (scores[scores.length - 1] ?? 0) : 0;
  const peak = scores.length > 0 ? Math.max(...scores) : 0;
  const level = Math.min(Math.max(status.input_level ?? 0, 0), 1);
  const speech = status.speech_probability ?? null;
  const speaking = speech !== null && speech >= SPEECH_THRESHOLD;

  return (
    <section className="panel" aria-label="Palabra de activación">
      <h2>Palabra de activación</h2>
      <p className="muted">
        Detectando «{status.phrase}» con umbral {threshold.toFixed(2)}.
      </p>

      {/*
        The level answers "does the microphone hear me at all?", which the
        detector score cannot: that stays at zero until the phrase is heard.
      */}
      <div className="meter-label">
        <span>Micrófono</span>
        {speech !== null && (
          <span className={speaking ? "voice-on" : "voice-off"}>
            {speaking ? "Voz detectada" : "Sin voz"}
          </span>
        )}
      </div>
      <div
        className="meter meter-level"
        role="meter"
        aria-valuenow={Number(level.toFixed(2))}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-label="Nivel del micrófono"
      >
        <div className="meter-fill" style={{ width: `${level * 100}%` }} />
      </div>
      {status.seconds_listening > 3 && level < SILENT_LEVEL && (
        <p className="muted small">
          No llega sonido del micrófono. Comprueba en Configuración que está elegido el
          dispositivo correcto y que no está silenciado en Windows.
        </p>
      )}

      <div className="meter-label">
        <span>Detector</span>
      </div>

      <ul className="guards">
        <li className={status.vad_enabled ? "guard-on" : "guard-off"}>
          {status.vad_enabled ? "Filtro de voz activo" : "Sin filtro de voz"}
        </li>
        {status.confirmation_frames !== null && status.confirmation_frames > 1 && (
          <li className="guard-on">
            Confirmación: {status.confirmation_frames} frames
          </li>
        )}
      </ul>

      <div
        className="meter"
        role="meter"
        aria-valuenow={Number(latest.toFixed(2))}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-label="Nivel actual del detector"
      >
        <div className="meter-fill" style={{ width: `${Math.min(latest, 1) * 100}%` }} />
        <div className="meter-threshold" style={{ left: `${Math.min(threshold, 1) * 100}%` }} />
      </div>

      <dl className="stats">
        <div>
          <dt>Activaciones</dt>
          <dd>{status.activations}</dd>
        </div>
        <div>
          <dt>Interrupciones</dt>
          <dd>{status.interruptions}</dd>
        </div>
        <div>
          <dt>Pico reciente</dt>
          <dd>{peak.toFixed(2)}</dd>
        </div>
        <div>
          <dt>Ecos descartados</dt>
          <dd>{status.echo_suppressions}</dd>
        </div>
        {/*
          Whether "Oye Chat" can cut an answer: what it needs while the
          assistant speaks, next to the best it has scored over an answer.
        */}
        {status.guarded_threshold !== null && status.guarded_threshold !== threshold && (
          <div>
            <dt>Umbral mientras responde</dt>
            <dd>{status.guarded_threshold.toFixed(2)}</dd>
          </div>
        )}
        {status.peak_score_while_speaking > 0 && (
          <div>
            <dt>Pico mientras respondía</dt>
            <dd>{status.peak_score_while_speaking.toFixed(2)}</dd>
          </div>
        )}
        <div>
          <dt>Escuchando</dt>
          <dd>{Math.round(status.seconds_listening)} s</dd>
        </div>
      </dl>
      {children}
    </section>
  );
}
