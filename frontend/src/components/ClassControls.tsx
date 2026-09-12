/** Start, pause and finish (spec section 6.1). */

import { CLASS_IS_RUNNING } from "../lib/states";
import type { SessionState } from "../lib/types";

interface Props {
  state: SessionState | null;
  busy: boolean;
  readyToStart: boolean;
  onPrepare: () => void;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onStop: () => void;
  onRecover: () => void;
}

export function ClassControls({
  state,
  busy,
  readyToStart,
  onPrepare,
  onStart,
  onPause,
  onResume,
  onStop,
  onRecover,
}: Props) {
  if (state === null) {
    return null;
  }

  const running = CLASS_IS_RUNNING.has(state);

  return (
    <section className="panel controls" aria-label="Control de la clase">
      {state === "ERROR" && (
        <button type="button" onClick={onRecover} disabled={busy}>
          Reintentar
        </button>
      )}

      {(state === "IDLE" || state === "STOPPED") && (
        <button type="button" onClick={onPrepare} disabled={busy}>
          Preparar sesión
        </button>
      )}

      {state === "READY" && (
        <button type="button" className="primary" onClick={onStart} disabled={busy || !readyToStart}>
          Iniciar clase
        </button>
      )}

      {state === "READY" && !readyToStart && (
        <p className="muted small">
          Ejecuta el diagnóstico y resuelve los fallos antes de iniciar la clase.
        </p>
      )}

      {running && (
        <button type="button" onClick={onPause} disabled={busy}>
          Pausar
        </button>
      )}

      {state === "PAUSED" && (
        <button type="button" className="primary" onClick={onResume} disabled={busy}>
          Reanudar
        </button>
      )}

      {(running || state === "PAUSED") && (
        <button type="button" className="secondary" onClick={onStop} disabled={busy}>
          Finalizar clase
        </button>
      )}
    </section>
  );
}
