/**
 * How each state is presented (spec sections 6.1 and 7).
 *
 * Spec section 19 asks that the real state always be visible. The label, the
 * colour and the microphone dot therefore all come from this one table, so
 * they cannot drift apart as the interface grows.
 */

import type { SessionState } from "./types";

export type Tone = "neutral" | "listening" | "active" | "busy" | "warning" | "danger";

export interface StatePresentation {
  label: string;
  description: string;
  tone: Tone;
  /** Mirrors MIC_ACTIVE_STATES in the backend state machine. */
  microphoneOpen: boolean;
}

export const STATE_PRESENTATION: Record<SessionState, StatePresentation> = {
  IDLE: {
    label: "Sin sesión",
    description: "No hay ninguna clase preparada.",
    tone: "neutral",
    microphoneOpen: false,
  },
  PREPARING: {
    label: "Preparando",
    description: "Cargando la configuración de la sesión.",
    tone: "busy",
    microphoneOpen: false,
  },
  READY: {
    label: "Preparada",
    description: "Todo listo. Pulsa «Iniciar clase» cuando quieras.",
    tone: "neutral",
    microphoneOpen: false,
  },
  PASSIVE_LISTENING: {
    label: "Escucha pasiva",
    description: "Esperando «Oye Chat». No se envía nada fuera del equipo.",
    tone: "listening",
    microphoneOpen: true,
  },
  ACTIVATED: {
    label: "Activado",
    description: "Se ha detectado la palabra de activación.",
    tone: "active",
    microphoneOpen: true,
  },
  CAPTURING_REQUEST: {
    label: "Escuchando la pregunta",
    description: "Capturando la intervención.",
    tone: "active",
    microphoneOpen: true,
  },
  THINKING: {
    label: "Pensando",
    description: "Preparando la respuesta.",
    tone: "busy",
    microphoneOpen: false,
  },
  SPEAKING: {
    label: "Respondiendo",
    description: "Reproduciendo la respuesta. Habla para interrumpir.",
    tone: "busy",
    microphoneOpen: false,
  },
  INTERRUPTED: {
    label: "Interrumpido",
    description: "La respuesta se ha cancelado.",
    tone: "warning",
    microphoneOpen: false,
  },
  PAUSED: {
    label: "En pausa",
    description: "El micrófono está cerrado.",
    tone: "warning",
    microphoneOpen: false,
  },
  STOPPED: {
    label: "Finalizada",
    description: "La sesión ha terminado.",
    tone: "neutral",
    microphoneOpen: false,
  },
  ERROR: {
    label: "Error",
    description: "Algo ha fallado. El micrófono está cerrado.",
    tone: "danger",
    microphoneOpen: false,
  },
};

/** Phase 0 has no conversation, so these are the states a class moves through. */
export const CLASS_IS_RUNNING: ReadonlySet<SessionState> = new Set<SessionState>([
  "PASSIVE_LISTENING",
  "ACTIVATED",
  "CAPTURING_REQUEST",
  "THINKING",
  "SPEAKING",
  "INTERRUPTED",
]);
