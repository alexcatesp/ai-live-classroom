import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { TurnView } from "../TurnView";
import { EMPTY_TURN, reduceTurn } from "../../lib/useBackend";
import type { TurnView as TurnViewData } from "../../lib/types";

function view(overrides: Partial<TurnViewData> = {}): TurnViewData {
  return { ...EMPTY_TURN, ...overrides };
}

describe("reduceTurn", () => {
  it("una activación nueva limpia la pregunta, la respuesta y el fallo anteriores", () => {
    const before = view({ question: "vieja", answer: "vieja", failure: "fallo" });
    const after = reduceTurn(before, { kind: "turn_started" });
    expect(after).toMatchObject({ question: "", answer: "", failure: null });
  });

  it("sigue la pregunta y la respuesta según llegan", () => {
    let current = reduceTurn(view(), { kind: "question", text: "¿Qué es HTML?", final: true });
    current = reduceTurn(current, { kind: "answer", text: "HTML es", final: false });
    expect(current).toMatchObject({ question: "¿Qué es HTML?", answer: "HTML es" });
  });

  it("recuerda el estado de la conexión con la IA y su motivo", () => {
    const current = reduceTurn(view(), {
      kind: "connection",
      state: "reconnecting",
      reason: "red perdida",
    });
    expect(current).toMatchObject({ realtime: "reconnecting", realtimeReason: "red perdida" });
  });
});

describe("TurnView", () => {
  it("no ocupa sitio cuando no hay nada que contar", () => {
    const { container } = render(
      <TurnView state="READY" turn={view()} busy={false} onStop={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("muestra la pregunta y la respuesta", () => {
    render(
      <TurnView
        state="SPEAKING"
        turn={view({ question: "¿Qué es HTML?", answer: "HTML es el lenguaje…" })}
        busy={false}
        onStop={() => {}}
      />,
    );
    expect(screen.getByText("¿Qué es HTML?")).toBeInTheDocument();
    expect(screen.getByText("HTML es el lenguaje…")).toBeInTheDocument();
  });

  it("ofrece «Parar» solo mientras hay una respuesta en marcha", async () => {
    const onStop = vi.fn();
    const { rerender } = render(
      <TurnView state="SPEAKING" turn={view()} busy={false} onStop={onStop} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Parar" }));
    expect(onStop).toHaveBeenCalledOnce();

    rerender(<TurnView state="PASSIVE_LISTENING" turn={view({ question: "x" })} busy={false} onStop={onStop} />);
    expect(screen.queryByRole("button", { name: "Parar" })).not.toBeInTheDocument();
  });

  it("dice claramente cuando la IA no está disponible", () => {
    render(
      <TurnView
        state="PASSIVE_LISTENING"
        turn={view({ realtime: "reconnecting", realtimeReason: "HTTP 503" })}
        busy={false}
        onStop={() => {}}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Sin conexión con la IA");
    expect(screen.getByRole("status")).toHaveTextContent("HTTP 503");
  });

  it("explica por qué no se respondió una activación", () => {
    render(
      <TurnView
        state="PASSIVE_LISTENING"
        turn={view({ failure: "Sin conexión con la API: la pregunta no se puede enviar." })}
        busy={false}
        onStop={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Sin conexión con la API");
  });
});
