/**
 * The spoken turn inside the state panel (plan-fase-1, H3): whether the AI is
 * reachable, what was asked, what is being answered, and the emergency stop.
 *
 * H5 turns this into the full transcript panel of D-11; for now it shows the
 * current or last question and answer.
 */

import type { SessionState, TurnView as TurnViewData } from "../lib/types";

interface Props {
  state: SessionState | null;
  turn: TurnViewData;
  busy: boolean;
  onStop: () => void;
}

const ANSWERING: ReadonlySet<SessionState> = new Set<SessionState>(["THINKING", "SPEAKING"]);

const REALTIME_LABEL: Record<string, string> = {
  ready: "Conectada con la IA",
  connecting: "Conectando con la IA…",
  reconnecting: "Sin conexión con la IA. Reintentando…",
  failed: "La IA ha rechazado la conexión",
  disconnected: "Sin conexión con la IA",
};

export function TurnView({ state, turn, busy, onStop }: Props) {
  const answering = state !== null && ANSWERING.has(state);
  const realtime = turn.realtime ? REALTIME_LABEL[turn.realtime] ?? turn.realtime : null;
  const unhealthy = turn.realtime !== null && turn.realtime !== "ready";

  if (!realtime && !turn.question && !turn.answer && !turn.failure && !answering) {
    return null;
  }

  return (
    <div className="turn-view">
      {realtime && (
        <p className={unhealthy ? "warning-line" : "muted small"} role="status">
          {realtime}
          {unhealthy && turn.realtimeReason ? `: ${turn.realtimeReason}` : ""}
        </p>
      )}

      {turn.failure && (
        <p className="warning-line" role="alert">
          {turn.failure}
        </p>
      )}

      {(turn.question || turn.answer) && (
        <dl className="conversation">
          <dt>Pregunta</dt>
          <dd>{turn.question || "…"}</dd>
          <dt>Respuesta</dt>
          <dd>{turn.answer || "…"}</dd>
        </dl>
      )}

      {answering && (
        <button type="button" className="secondary" disabled={busy} onClick={onStop}>
          Parar
        </button>
      )}
    </div>
  );
}
