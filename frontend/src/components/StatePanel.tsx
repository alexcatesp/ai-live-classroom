/** The state indicator (spec section 6.1): always visible, never guessed. */

import { STATE_PRESENTATION } from "../lib/states";
import type { StateSnapshot } from "../lib/types";

interface Props {
  snapshot: StateSnapshot | null;
  connected: boolean;
}

export function StatePanel({ snapshot, connected }: Props) {
  if (!snapshot) {
    return (
      <section className="panel state-panel" aria-label="Estado de la sesión">
        <p className="muted">Conectando con el motor local…</p>
      </section>
    );
  }

  const presentation = STATE_PRESENTATION[snapshot.state];

  return (
    <section className="panel state-panel" aria-label="Estado de la sesión">
      <div className={`state-badge tone-${presentation.tone}`}>
        <span className="state-label">{presentation.label}</span>
        <span className="state-code">{snapshot.state}</span>
      </div>
      <p className="state-description">{presentation.description}</p>

      <div
        className={`mic-indicator ${snapshot.microphone_active ? "mic-on" : "mic-off"}`}
        role="status"
        aria-live="polite"
      >
        <span className="mic-dot" aria-hidden="true" />
        {snapshot.microphone_active ? "Micrófono abierto" : "Micrófono cerrado"}
      </div>

      {!connected && (
        <p className="warning-line" role="alert">
          Sin conexión con el motor local. Reintentando…
        </p>
      )}
    </section>
  );
}
