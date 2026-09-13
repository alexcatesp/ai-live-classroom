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
  turn_silence_ms: number;
  transcription_model: string | null;
  realtime_noise_reduction: string | null;
  history_turns: number;
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
  /** Loudest microphone level of the last half second, 0..1. */
  input_level: number;
  /** Silero's speech probability, or null when the voice filter is off. */
  speech_probability: number | null;
}

export type TakeKind = "phrase" | "near_miss" | "speech";

export interface TakeSummary {
  seconds: number;
  level: number;
}

/** One model measured on the same audio as the other. */
export interface ModelScore {
  phrase_detected: number;
  near_misses_triggered: number;
  speech_activations: number;
  base_detection: number;
  base_false_rate: number;
}

export interface VoiceTrainingResult {
  original: ModelScore;
  tuned: ModelScore;
  phrase_total: number;
  near_misses_total: number;
  speech_held_out_seconds: number;
  recommended: boolean;
  verdict: string;
  seconds: number;
}

export interface VoiceStatus {
  phrase: string;
  near_miss_prompts: string[];
  phrase_takes: (TakeSummary | null)[];
  near_miss_takes: (TakeSummary | null)[];
  speech_take: TakeSummary | null;
  take_seconds: Record<TakeKind, number>;
  recording: boolean;
  state: "idle" | "training" | "ready" | "failed";
  progress: number;
  message: string;
  error: string | null;
  result: VoiceTrainingResult | null;
  personal_model: boolean;
  unavailable_reason: string | null;
}

export interface ConversationTestResult {
  question: string;
  answer: string;
  session_open_seconds: number;
  /** From the question ending to the first sound of the answer. */
  first_audio_seconds: number | null;
  silence_seconds: number;
  answer_seconds: number;
  status: string | null;
  usage: Record<string, unknown> | null;
}

export interface ConversationTestStatus {
  state: "idle" | "connecting" | "listening" | "waiting" | "answering" | "done" | "failed";
  error: string | null;
  question: string;
  answer: string;
  result: ConversationTestResult | null;
  can_play: boolean;
  playing: boolean;
}

export type BackendEvent =
  | { type: "state"; payload: Transition | StateSnapshot }
  | { type: "wakeword"; payload: { phrase: string; score: number; at: string } };
