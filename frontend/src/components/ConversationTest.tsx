/**
 * "Probar conversación": one real question to the Realtime API, from the
 * application, with the key saved in settings (plan-fase-1, H1).
 *
 * It stands in for the spoken turn until H3 wires it into the class, and it
 * shows the numbers the rest of Phase 1 depends on: how long the session takes
 * to open and how long the first sound of an answer takes.
 */

import { useCallback, useEffect, useId, useState } from "react";

import type { BackendClient } from "../lib/api";
import type { ConversationTestStatus } from "../lib/types";

interface Props {
  client: BackendClient | null;
  /** The class holds the microphone while it listens. */
  classListening: boolean;
}

const RUNNING = new Set(["connecting", "listening", "waiting", "answering"]);

const STEP: Record<string, string> = {
  connecting: "Abriendo la sesión con la API…",
  listening: "Habla ahora. La pregunta termina sola cuando dejes de hablar un par de segundos.",
  waiting: "Pregunta recibida. Esperando la respuesta…",
  answering: "Recibiendo la respuesta…",
};

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function tokens(usage: Record<string, unknown> | null): string | null {
  const total = usage?.["total_tokens"];
  return typeof total === "number" ? `${total} tokens` : null;
}

export function ConversationTest({ client, classListening }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [status, setStatus] = useState<ConversationTestStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const contentId = useId();

  const refresh = useCallback(async () => {
    if (!client) return;
    try {
      setStatus(await client.getConversationTest());
    } catch (failure) {
      setError(describe(failure));
    }
  }, [client]);

  useEffect(() => {
    if (expanded) void refresh();
  }, [expanded, refresh]);

  const running = status !== null && (RUNNING.has(status.state) || status.playing);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void refresh(), 400);
    return () => clearInterval(timer);
  }, [running, refresh]);

  const act = async (action: () => Promise<ConversationTestStatus>) => {
    setError(null);
    try {
      setStatus(await action());
    } catch (failure) {
      setError(describe(failure));
    }
  };

  const result = status?.state === "done" ? status.result : null;

  return (
    <section className="panel" aria-label="Probar conversación">
      <header className="panel-header">
        <h2>
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
            Probar conversación
          </button>
        </h2>
      </header>

      <div id={contentId} hidden={!expanded}>
        <p className="muted small">
          Haz una pregunta en voz alta y escucha la respuesta de la API, con el micrófono y la
          clave de la configuración. Es una prueba: en clase la conversación empezará con «Oye
          Chat». Cada pregunta consume unos céntimos.
        </p>

        {classListening && (
          <p className="warning-line">
            El micrófono está escuchando la clase. Pausa o finaliza la clase para probar.
          </p>
        )}
        {error && (
          <p className="warning-line" role="alert">
            {error}
          </p>
        )}

        <div className="controls">
          {running && status && !status.playing ? (
            <button
              type="button"
              className="secondary"
              onClick={() => client && void act(() => client.cancelConversationTest())}
            >
              Cancelar
            </button>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={!client || classListening || running}
              onClick={() => client && void act(() => client.startConversationTest())}
            >
              {result ? "Otra pregunta" : "Hacer una pregunta"}
            </button>
          )}
          {status?.can_play && (
            <button
              type="button"
              disabled={status.playing}
              onClick={() => client && void act(() => client.playConversationTest())}
            >
              {status.playing ? "Reproduciendo…" : "Escuchar la respuesta"}
            </button>
          )}
        </div>

        {status && RUNNING.has(status.state) && (
          <p className="conversation-step" role="status" aria-live="polite">
            {STEP[status.state]}
          </p>
        )}

        {status?.state === "failed" && status.error && (
          <p className="summary-failed" role="alert">
            {status.error}
          </p>
        )}

        {status && (status.question || status.answer) && (
          <dl className="conversation">
            <dt>Pregunta</dt>
            <dd>{status.question || "…"}</dd>
            <dt>Respuesta</dt>
            <dd>{status.answer || "…"}</dd>
          </dl>
        )}

        {result && (
          <dl className="stats">
            <div>
              <dt>Abrir la sesión</dt>
              <dd>{result.session_open_seconds.toFixed(2)} s</dd>
            </div>
            <div>
              <dt>Hasta el primer audio</dt>
              <dd>
                {result.first_audio_seconds === null
                  ? "—"
                  : `${result.first_audio_seconds.toFixed(2)} s`}
              </dd>
            </div>
            <div>
              <dt>Silencio de fin de pregunta</dt>
              <dd>{result.silence_seconds.toFixed(1)} s</dd>
            </div>
            <div>
              <dt>Duración de la respuesta</dt>
              <dd>{result.answer_seconds.toFixed(1)} s</dd>
            </div>
            {tokens(result.usage) && (
              <div>
                <dt>Consumo</dt>
                <dd>{tokens(result.usage)}</dd>
              </div>
            )}
          </dl>
        )}
        {result && (
          <p className="muted small">
            «Hasta el primer audio» cuenta desde que la API da la pregunta por terminada, es decir,
            ya descontado el silencio: es lo que tardan la red y el modelo. Lo que espera quien
            pregunta es la suma de los dos.
          </p>
        )}
      </div>
    </section>
  );
}
