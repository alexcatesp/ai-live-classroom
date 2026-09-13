import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { VoiceTraining } from "../VoiceTraining";
import type { BackendClient } from "../../lib/api";
import type { VoiceStatus } from "../../lib/types";

const take = { seconds: 1.1, level: 0.7 };

function voice(overrides: Partial<VoiceStatus> = {}): VoiceStatus {
  return {
    phrase: "Oye Chat",
    near_miss_prompts: ["Oye chico", "Oye Chechu", "Oye cat", "Oye, ¿qué tal?", "Chat"],
    phrase_takes: [null, null, null, null, null],
    near_miss_takes: [null, null, null, null, null],
    take_seconds: 3,
    recording: false,
    state: "idle",
    progress: 0,
    message: "",
    error: null,
    result: null,
    personal_model: false,
    unavailable_reason: null,
    ...overrides,
  };
}

function fakeClient(status: VoiceStatus, extra: Partial<Record<keyof BackendClient, unknown>> = {}) {
  return {
    getVoice: vi.fn().mockResolvedValue(status),
    recordTake: vi.fn().mockResolvedValue(status),
    forgetTake: vi.fn().mockResolvedValue(status),
    trainVoice: vi.fn().mockResolvedValue(status),
    acceptVoiceModel: vi.fn().mockResolvedValue(voice()),
    discardVoiceModel: vi.fn().mockResolvedValue(voice()),
    restoreOriginalModel: vi.fn().mockResolvedValue(voice()),
    ...extra,
  } as unknown as BackendClient & Record<string, ReturnType<typeof vi.fn>>;
}

async function open(client: BackendClient, classListening = false) {
  render(<VoiceTraining client={client} classListening={classListening} />);
  await userEvent.click(screen.getByRole("button", { name: "Entrenar con mi voz" }));
  await screen.findByText(/Frases parecidas/);
}

describe("VoiceTraining", () => {
  it("empieza plegado bajo su botón", () => {
    render(<VoiceTraining client={fakeClient(voice())} classListening={false} />);
    expect(screen.getByRole("button", { name: "Entrenar con mi voz" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("pide cinco veces la frase y cada frase parecida", async () => {
    await open(fakeClient(voice()));
    expect(screen.getByRole("button", { name: "Grabar: Vez 5" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Grabar: «Oye Chechu»" })).toBeInTheDocument();
  });

  it("explica antes de grabar que las grabaciones se borran y no salen del equipo", async () => {
    await open(fakeClient(voice()));
    expect(screen.getByText(/no salen de él y se borran/)).toBeInTheDocument();
  });

  it("graba la toma pedida", async () => {
    const client = fakeClient(voice());
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Grabar: Vez 2" }));
    expect(client.recordTake).toHaveBeenCalledWith("phrase", 1);
  });

  it("enseña junto a la toma por qué no valía", async () => {
    const client = fakeClient(voice(), {
      recordTake: vi.fn().mockRejectedValue(new Error("Apenas se oye nada.")),
    });
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Grabar: «Oye cat»" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Apenas se oye nada.");
  });

  it("no deja entrenar hasta tener todas las grabaciones", async () => {
    await open(fakeClient(voice({ phrase_takes: [take, take, take, take, take] })));
    expect(screen.getByRole("button", { name: "Entrenar" })).toBeDisabled();
  });

  it("entrena cuando está todo grabado", async () => {
    const all = voice({
      phrase_takes: [take, take, take, take, take],
      near_miss_takes: [take, take, take, take, take],
    });
    const client = fakeClient(all);
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Entrenar" }));
    expect(client.trainVoice).toHaveBeenCalledOnce();
  });

  it("no deja grabar mientras la clase tiene el micrófono", async () => {
    await open(fakeClient(voice()), true);
    expect(screen.getByText(/Pausa o finaliza la clase para grabar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Grabar: Vez 1" })).toBeDisabled();
  });

  it("muestra el progreso mientras entrena", async () => {
    await open(fakeClient(voice({ state: "training", progress: 0.4, message: "Entrenando" })));
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
  });

  it("presenta el resultado y deja usar o descartar el modelo", async () => {
    const client = fakeClient(
      voice({
        state: "ready",
        result: {
          phrase_detected: 5,
          phrase_total: 5,
          near_misses_triggered: 1,
          near_misses_total: 5,
          held_out_detection: 0.97,
          held_out_false_rate: 0.002,
          seconds: 80,
        },
      }),
    );
    await open(client);
    expect(screen.getByText(/Reconoce 5 de 5/)).toBeInTheDocument();
    expect(screen.getByText(/Se activa con 1 de 5/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Usar este modelo" }));
    await waitFor(() => expect(client.acceptVoiceModel).toHaveBeenCalledOnce());
  });

  it("permite volver al modelo original", async () => {
    const client = fakeClient(voice({ personal_model: true }));
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Volver al modelo original" }));
    expect(client.restoreOriginalModel).toHaveBeenCalledOnce();
  });

  it("explica por qué no se puede entrenar en esta instalación", async () => {
    render(
      <VoiceTraining
        client={fakeClient(voice({ unavailable_reason: "Falta el corpus base del detector." }))}
        classListening={false}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Entrenar con mi voz" }));
    expect(await screen.findByText(/Falta el corpus base/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Entrenar" })).not.toBeInTheDocument();
  });
});
