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
    description:
      "La pregunta se está enviando a la IA. Termina sola cuando dejes de hablar un par de segundos.",
    tone: "active",
    microphoneOpen: true,
  },
  THINKING: {
    label: "Pensando",
    description:
      "Pregunta recibida. Preparando la respuesta. El micrófono sigue abierto, solo para oír «Oye Chat».",
    tone: "busy",
    microphoneOpen: true,
  },
  SPEAKING: {
    label: "Respondiendo",
    description:
      "Reproduciendo la respuesta. Di «Oye Chat» y tu nueva pregunta, o pulsa «Parar», para cortarla.",
    tone: "busy",
    microphoneOpen: true,
  },
  INTERRUPTED: {
    label: "Interrumpido",
    description: "La respuesta se ha cancelado.",
    tone: "warning",
    microphoneOpen: true,
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

/** The states a running class moves through. */
export const CLASS_IS_RUNNING: ReadonlySet<SessionState> = new Set<SessionState>([
  "PASSIVE_LISTENING",
  "ACTIVATED",
  "CAPTURING_REQUEST",
  "THINKING",
  "SPEAKING",
  "INTERRUPTED",
]);
