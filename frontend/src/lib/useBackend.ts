/** Keeps the interface in step with the backend over the event socket. */

import { useCallback, useEffect, useRef, useState } from "react";

import { BackendClient } from "./api";
import type { StateSnapshot, Transition, TurnEvent, TurnView } from "./types";

function isSnapshot(payload: Transition | StateSnapshot): payload is StateSnapshot {
  return "state" in payload;
}

export interface Connection {
  snapshot: StateSnapshot | null;
  connected: boolean;
  lastActivation: { phrase: string; score: number; at: string } | null;
  /** The current or last spoken turn, as text (H3). */
  turn: TurnView;
  refresh: () => Promise<void>;
}

export const EMPTY_TURN: TurnView = {
  question: "",
  answer: "",
  failure: null,
  realtime: null,
  realtimeReason: null,
};

/** Applies one "turn" event from the backend to what the interface shows. */
export function reduceTurn(view: TurnView, event: TurnEvent): TurnView {
  switch (event.kind) {
    case "turn_started":
      return { ...view, question: "", answer: "", failure: null };
    case "question":
      return { ...view, question: event.text ?? "" };
    case "answer":
      return { ...view, answer: event.text ?? "" };
    case "turn_failed":
      return { ...view, failure: event.reason ?? "La pregunta no pudo responderse." };
    case "connection":
      return { ...view, realtime: event.state ?? null, realtimeReason: event.reason ?? null };
    default:
      return view;
  }
}

export function useBackendConnection(client: BackendClient | null): Connection {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastActivation, setLastActivation] = useState<Connection["lastActivation"]>(null);
  const [turn, setTurn] = useState<TurnView>(EMPTY_TURN);
  const socketRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    if (!client) return;
    try {
      setSnapshot(await client.getState());
      setConnected(true);
    } catch {
      // Every caller fires this without awaiting, so an uncaught rejection
      // here becomes an unhandled promise rejection in the running
      // application. The socket's reconnect loop is what recovers from a
      // backend that went away; this just stops shouting about it and leaves
      // the last known state on screen.
      setConnected(false);
    }
  }, [client]);

  useEffect(() => {
    if (!client) return;
    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      const socket = client.events();
      socketRef.current = socket;

      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        const event = JSON.parse(message.data as string);
        if (event.type === "state") {
          if (isSnapshot(event.payload)) {
            setSnapshot(event.payload);
          } else {
            // A transition carries only the move, so the full snapshot -- which
            // events are now possible -- is fetched alongside it.
            void refresh();
          }
        } else if (event.type === "wakeword") {
          setLastActivation(event.payload);
        } else if (event.type === "turn") {
          setTurn((view) => reduceTurn(view, event.payload as TurnEvent));
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!closed) {
          // The backend restarting must not leave a dead window behind.
          retry = setTimeout(connect, 1000);
        }
      };
      socket.onerror = () => socket.close();
    };

    connect();
    void refresh();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      socketRef.current?.close();
    };
  }, [client, refresh]);

  return { snapshot, connected, lastActivation, turn, refresh };
}
