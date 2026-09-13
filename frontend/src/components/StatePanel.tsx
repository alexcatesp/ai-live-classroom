/** The state indicator (spec section 6.1): always visible, never guessed. */

import type { ReactNode } from "react";

import { STATE_PRESENTATION } from "../lib/states";
import type { StateSnapshot } from "../lib/types";

interface Props {
  snapshot: StateSnapshot | null;
  connected: boolean;
  /** The class controls, kept beside the state they change. */
  children?: ReactNode;
}

export function StatePanel({ snapshot, connected, children }: Props) {
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
      {/* The technical code stays out of sight: the teacher reads Spanish, and
          anyone debugging finds it in data-state. */}
      <div className={`state-badge tone-${presentation.tone}`} data-state={snapshot.state}>
        <span className="state-label">{presentation.label}</span>
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

      {children && <div className="state-controls">{children}</div>}
    </section>
  );
}
