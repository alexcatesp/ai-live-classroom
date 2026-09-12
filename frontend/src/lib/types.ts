/** Mirrors the backend schemas in backend/src/aiclassroom/api/schemas.py. */

export type SessionState =
  | "IDLE"
  | "PREPARING"
  | "READY"
  | "PASSIVE_LISTENING"
  | "ACTIVATED"
  | "CAPTURING_REQUEST"
  | "THINKING"
  | "SPEAKING"
  | "INTERRUPTED"
  | "PAUSED"
  | "STOPPED"
  | "ERROR";

export interface Transition {
  source: SessionState;
  event: string;
  target: SessionState;
  at: string;
  reason: string | null;
}

export interface StateSnapshot {
  state: SessionState;
  microphone_active: boolean;
  available_events: string[];
  history: Transition[];
}

export type CheckStatus = "ok" | "warning" | "failed" | "skipped";

export interface CheckResult {
  id: string;
  label: string;
  status: CheckStatus;
  detail: string;
  remedy: string | null;
  at: string;
}

export interface DiagnosticsReport {
  status: CheckStatus;
  ready_to_start: boolean;
  duration_seconds: number;
  started_at: string;
  finished_at: string | null;
  results: CheckResult[];
}

export interface Settings {
  realtime_model: string;
  voice: string;
  input_device: string | null;
  output_device: string | null;
  wake_phrase: string;
  wake_sensitivity: number;
  wake_refractory_seconds: number;
  wake_vad_threshold: number;
  wake_confirmation_frames: number;
  echo_guard_margin: number;
  max_response_seconds: number;
  materials_dir: string | null;
  transcript_retention: "discard" | "session_only" | "keep";
  daily_cost_limit_eur: number | null;
  session_cost_limit_eur: number | null;
}

export interface SettingsResponse {
  settings: Settings;
  api_key_configured: boolean;
  /** The stored key is sealed with a passphrase, so it travels between PCs. */
  requires_passphrase: boolean;
  /** Whether the key can be read right now. */
  unlocked: boolean;
}

export interface AudioDevice {
  index: number;
  name: string;
  channels: number;
  is_default: boolean;
}

export interface DeviceInventory {
  inputs: AudioDevice[];
  outputs: AudioDevice[];
  error: string | null;
}

export interface ListeningStatus {
  listening: boolean;
  activations: number;
  interruptions: number;
  echo_suppressions: number;
  frames_processed: number;
  seconds_listening: number;
  threshold: number | null;
  recent_scores: number[];
  phrase: string | null;
  vad_enabled: boolean;
  confirmation_frames: number | null;
}

export type BackendEvent =
  | { type: "state"; payload: Transition | StateSnapshot }
  | { type: "wakeword"; payload: { phrase: string; score: number; at: string } };
