import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatePanel } from "../StatePanel";
import type { SessionState, StateSnapshot } from "../../lib/types";

function snapshot(state: SessionState, microphone: boolean): StateSnapshot {
  return {
    state,
    microphone_active: microphone,
    available_events: [],
    history: [],
  };
}

describe("StatePanel", () => {
  it("shows the state in Spanish and its technical code", () => {
    render(<StatePanel snapshot={snapshot("PASSIVE_LISTENING", true)} connected />);
    expect(screen.getByText("Escucha pasiva")).toBeInTheDocument();
    expect(screen.getByText("PASSIVE_LISTENING")).toBeInTheDocument();
  });

  it("announces an open microphone", () => {
    render(<StatePanel snapshot={snapshot("PASSIVE_LISTENING", true)} connected />);
    expect(screen.getByRole("status")).toHaveTextContent("Micrófono abierto");
  });

  it("announces a closed microphone in the error state", () => {
    // Spec section 19: an error must never look like listening.
    render(<StatePanel snapshot={snapshot("ERROR", false)} connected />);
    expect(screen.getByRole("status")).toHaveTextContent("Micrófono cerrado");
  });

  it("follows the backend rather than the state name when they disagree", () => {
    // The backend owns the truth; the panel must not infer it independently.
    render(<StatePanel snapshot={snapshot("PASSIVE_LISTENING", false)} connected />);
    expect(screen.getByRole("status")).toHaveTextContent("Micrófono cerrado");
  });

  it("warns when the local engine is unreachable", () => {
    render(<StatePanel snapshot={snapshot("READY", false)} connected={false} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Sin conexión");
  });

  it("says it is connecting before the first snapshot arrives", () => {
    render(<StatePanel snapshot={null} connected={false} />);
    expect(screen.getByText(/Conectando con el motor local/)).toBeInTheDocument();
  });
});
