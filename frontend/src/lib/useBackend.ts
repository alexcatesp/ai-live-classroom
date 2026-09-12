/** Keeps the interface in step with the backend over the event socket. */

import { useCallback, useEffect, useRef, useState } from "react";

import { BackendClient } from "./api";
import type { StateSnapshot, Transition } from "./types";

function isSnapshot(payload: Transition | StateSnapshot): payload is StateSnapshot {
  return "state" in payload;
}

export interface Connection {
  snapshot: StateSnapshot | null;
  connected: boolean;
  lastActivation: { phrase: string; score: number; at: string } | null;
  refresh: () => Promise<void>;
}

export function useBackendConnection(client: BackendClient | null): Connection {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const [lastActivation, setLastActivation] = useState<Connection["lastActivation"]>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    if (!client) return;
    setSnapshot(await client.getState());
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

  return { snapshot, connected, lastActivation, refresh };
}
