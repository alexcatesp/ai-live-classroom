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
    speech_take: null,
    take_seconds: { phrase: 3, near_miss: 3, speech: 30 },
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

  it("pide también medio minuto hablando con normalidad", async () => {
    const client = fakeClient(voice());
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Grabar: Medio minuto hablando" }));
    expect(client.recordTake).toHaveBeenCalledWith("speech", 0);
  });

  it("no deja entrenar sin el medio minuto de habla", async () => {
    await open(
      fakeClient(
        voice({
          phrase_takes: [take, take, take, take, take],
          near_miss_takes: [take, take, take, take, take],
        }),
      ),
    );
    expect(screen.getByRole("button", { name: "Entrenar" })).toBeDisabled();
  });

  it("entrena cuando está todo grabado", async () => {
    const all = voice({
      phrase_takes: [take, take, take, take, take],
      near_miss_takes: [take, take, take, take, take],
      speech_take: { seconds: 30, level: 0.6 },
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

  const comparison = (recommended: boolean) => ({
    original: {
      phrase_detected: 2,
      near_misses_triggered: 3,
      speech_activations: 1,
      base_detection: 0.95,
      base_false_rate: 0.002,
    },
    tuned: {
      phrase_detected: 5,
      near_misses_triggered: 0,
      speech_activations: recommended ? 0 : 3,
      base_detection: 0.96,
      base_false_rate: 0.001,
    },
    phrase_total: 5,
    near_misses_total: 5,
    speech_held_out_seconds: 10.5,
    recommended,
    verdict: recommended ? "Recomendado: reconoce tu frase 5 de 5 veces." : "No se recomienda.",
    seconds: 40,
  });

  it("compara el modelo ajustado con el original, fila a fila", async () => {
    await open(fakeClient(voice({ state: "ready", result: comparison(true) })));
    const row = screen.getByRole("rowheader", { name: /Reconoce tu «Oye Chat»/ }).closest("tr")!;
    expect(row).toHaveTextContent("2 de 5");
    expect(row).toHaveTextContent("5 de 5");
    expect(screen.getByText(/Recomendado/)).toBeInTheDocument();
  });

  it("si lo recomienda, lo principal es usarlo", async () => {
    const client = fakeClient(voice({ state: "ready", result: comparison(true) }));
    await open(client);
    await userEvent.click(screen.getByRole("button", { name: "Usar este modelo" }));
    await waitFor(() => expect(client.acceptVoiceModel).toHaveBeenCalledOnce());
  });

  it("si no lo recomienda, lo principal es quedarse con el original", async () => {
    const client = fakeClient(voice({ state: "ready", result: comparison(false) }));
    await open(client);
    expect(screen.queryByRole("button", { name: "Usar este modelo" })).not.toBeInTheDocument();
    const keep = screen.getByRole("button", { name: "Descartar y seguir con el original" });
    expect(keep).toHaveClass("primary");
    await userEvent.click(keep);
    await waitFor(() => expect(client.discardVoiceModel).toHaveBeenCalledOnce());
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
