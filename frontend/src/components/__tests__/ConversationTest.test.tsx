import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConversationTest } from "../ConversationTest";
import type { BackendClient } from "../../lib/api";
import type { ConversationTestStatus } from "../../lib/types";

function status(overrides: Partial<ConversationTestStatus> = {}): ConversationTestStatus {
  return {
    state: "idle",
    error: null,
    question: "",
    answer: "",
    result: null,
    can_play: false,
    playing: false,
    ...overrides,
  };
}

function fakeClient(current: ConversationTestStatus, extra: Record<string, unknown> = {}) {
  return {
    getConversationTest: vi.fn().mockResolvedValue(current),
    startConversationTest: vi.fn().mockResolvedValue(status({ state: "connecting" })),
    cancelConversationTest: vi.fn().mockResolvedValue(status()),
    playConversationTest: vi.fn().mockResolvedValue(current),
    ...extra,
  } as unknown as BackendClient & Record<string, ReturnType<typeof vi.fn>>;
}

async function open(client: BackendClient, classListening = false) {
  render(<ConversationTest client={client} classListening={classListening} />);
  await userEvent.click(screen.getByRole("button", { name: "Probar conversación" }));
}

const done = status({
  state: "done",
  question: "¿Qué es HTML?",
  answer: "HTML es el lenguaje de marcado de la web.",
  can_play: true,
  result: {
    question: "¿Qué es HTML?",
    answer: "HTML es el lenguaje de marcado de la web.",
    session_open_seconds: 0.84,
    first_audio_seconds: 0.62,
    silence_seconds: 2,
    answer_seconds: 4.3,
    status: "completed",
    usage: { total_tokens: 312 },
  },
});

describe("ConversationTest", () => {
  it("empieza plegado", () => {
    render(<ConversationTest client={fakeClient(status())} classListening={false} />);
    expect(screen.getByRole("button", { name: "Probar conversación" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("lanza una pregunta", async () => {
    const client = fakeClient(status());
    await open(client);
    await userEvent.click(await screen.findByRole("button", { name: "Hacer una pregunta" }));
    expect(client.startConversationTest).toHaveBeenCalledOnce();
  });

  it("dice cuándo hablar", async () => {
    await open(fakeClient(status({ state: "listening" })));
    expect(await screen.findByText(/Habla ahora/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeInTheDocument();
  });

  it("muestra la pregunta, la respuesta y los tiempos", async () => {
    await open(fakeClient(done));
    expect(await screen.findByText("¿Qué es HTML?")).toBeInTheDocument();
    expect(screen.getByText(/lenguaje de marcado/)).toBeInTheDocument();
    expect(screen.getByText("Hasta el primer audio").nextSibling).toHaveTextContent("0.62 s");
    expect(screen.getByText("Abrir la sesión").nextSibling).toHaveTextContent("0.84 s");
    expect(screen.getByText("Consumo").nextSibling).toHaveTextContent("312 tokens");
  });

  it("deja escuchar la respuesta", async () => {
    const client = fakeClient(done);
    await open(client);
    await userEvent.click(await screen.findByRole("button", { name: "Escuchar la respuesta" }));
    expect(client.playConversationTest).toHaveBeenCalledOnce();
  });

  it("explica por qué no pudo empezar", async () => {
    const client = fakeClient(status(), {
      startConversationTest: vi
        .fn()
        .mockRejectedValue(new Error("No hay ninguna clave de la API guardada.")),
    });
    await open(client);
    await userEvent.click(await screen.findByRole("button", { name: "Hacer una pregunta" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No hay ninguna clave");
  });

  it("no deja probar mientras la clase tiene el micrófono", async () => {
    await open(fakeClient(status()), true);
    expect(await screen.findByRole("button", { name: "Hacer una pregunta" })).toBeDisabled();
  });

  it("muestra el fallo de la prueba", async () => {
    await open(fakeClient(status({ state: "failed", error: "La API rechazó la clave (HTTP 401)." })));
    expect(await screen.findByText(/rechazó la clave/)).toBeInTheDocument();
  });
});
