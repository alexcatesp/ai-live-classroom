import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WakeWordMeter } from "../WakeWordMeter";
import type { ListeningStatus } from "../../lib/types";

function status(overrides: Partial<ListeningStatus> = {}): ListeningStatus {
  return {
    listening: true,
    activations: 3,
    interruptions: 1,
    echo_suppressions: 0,
    frames_processed: 500,
    seconds_listening: 42.4,
    threshold: 0.65,
    recent_scores: [0.1, 0.2, 0.8],
    phrase: "Oye Chat",
    vad_enabled: true,
    confirmation_frames: 2,
    input_level: 0.6,
    speech_probability: 0.9,
    guarded_threshold: 0.8,
    peak_score_while_speaking: 0,
    ...overrides,
  };
}

describe("WakeWordMeter", () => {
  it("waits until the class is listening", () => {
    render(<WakeWordMeter status={null} />);
    expect(screen.getByText(/escucha pasiva/)).toBeInTheDocument();
  });

  it("shows the phrase and the threshold in use", () => {
    render(<WakeWordMeter status={status()} />);
    expect(screen.getByText(/«Oye Chat»/)).toBeInTheDocument();
    expect(screen.getByText(/0\.65/)).toBeInTheDocument();
  });

  it("reports the latest score on the meter", () => {
    // This is the number the sensitivity is tuned against in a real class (R-1).
    render(<WakeWordMeter status={status()} />);
    expect(screen.getByRole("meter", { name: "Nivel actual del detector" })).toHaveAttribute("aria-valuenow", "0.8");
  });

  it("counts activations and interruptions separately", () => {
    render(<WakeWordMeter status={status()} />);
    expect(screen.getByText("Activaciones").nextSibling).toHaveTextContent("3");
    expect(screen.getByText("Interrupciones").nextSibling).toHaveTextContent("1");
  });

  it("shows the recent peak, which is what a false positive looks like", () => {
    render(<WakeWordMeter status={status({ recent_scores: [0.1, 0.93, 0.2] })} />);
    expect(screen.getByText("Pico reciente").nextSibling).toHaveTextContent("0.93");
  });

  it("copes with an empty score history", () => {
    render(<WakeWordMeter status={status({ recent_scores: [] })} />);
    expect(screen.getByRole("meter", { name: "Nivel actual del detector" })).toHaveAttribute("aria-valuenow", "0");
  });
});

// -- the defences shown live (risks R-1 and R-6) -------------------------

describe("WakeWordMeter, defensas", () => {
  it("muestra que el filtro de voz está activo", () => {
    render(<WakeWordMeter status={status()} />);
    expect(screen.getByText("Filtro de voz activo")).toBeInTheDocument();
  });

  it("avisa cuando el filtro de voz está desactivado", () => {
    render(<WakeWordMeter status={status({ vad_enabled: false })} />);
    expect(screen.getByText("Sin filtro de voz")).toBeInTheDocument();
  });

  it("muestra la confirmación por frames cuando se exige más de uno", () => {
    render(<WakeWordMeter status={status({ confirmation_frames: 3 })} />);
    expect(screen.getByText(/Confirmación: 3 frames/)).toBeInTheDocument();
  });

  it("no muestra la confirmación cuando basta un frame", () => {
    render(<WakeWordMeter status={status({ confirmation_frames: 1 })} />);
    expect(screen.queryByText(/Confirmación/)).not.toBeInTheDocument();
  });

  it("cuenta los ecos descartados, que es cómo se ajusta el margen", () => {
    render(<WakeWordMeter status={status({ echo_suppressions: 4 })} />);
    expect(screen.getByText("Ecos descartados").nextSibling).toHaveTextContent("4");
  });
});

// -- does the microphone hear anything at all? ----------------------------

describe("WakeWordMeter, micrófono", () => {
  it("muestra el nivel del micrófono aparte del detector", () => {
    render(<WakeWordMeter status={status({ input_level: 0.42 })} />);
    expect(screen.getByRole("meter", { name: "Nivel del micrófono" })).toHaveAttribute(
      "aria-valuenow",
      "0.42",
    );
  });

  it("dice cuándo el filtro de voz oye hablar", () => {
    render(<WakeWordMeter status={status({ speech_probability: 0.9 })} />);
    expect(screen.getByText("Voz detectada")).toBeInTheDocument();
  });

  it("dice cuándo no oye voz", () => {
    render(<WakeWordMeter status={status({ speech_probability: 0.1 })} />);
    expect(screen.getByText("Sin voz")).toBeInTheDocument();
  });

  it("no opina sobre la voz si el filtro está desactivado", () => {
    render(<WakeWordMeter status={status({ speech_probability: null })} />);
    expect(screen.queryByText("Voz detectada")).not.toBeInTheDocument();
    expect(screen.queryByText("Sin voz")).not.toBeInTheDocument();
  });

  it("avisa si tras unos segundos no llega sonido", () => {
    render(<WakeWordMeter status={status({ input_level: 0, seconds_listening: 10 })} />);
    expect(screen.getByText(/No llega sonido del micrófono/)).toBeInTheDocument();
  });

  it("no avisa nada más empezar, antes de que llegue audio", () => {
    render(<WakeWordMeter status={status({ input_level: 0, seconds_listening: 1 })} />);
    expect(screen.queryByText(/No llega sonido/)).not.toBeInTheDocument();
  });
  it("muestra lo que necesita «Oye Chat» mientras responde y lo mejor que ha oído", () => {
    render(
      <WakeWordMeter status={status({ guarded_threshold: 0.95, peak_score_while_speaking: 0.91 })} />,
    );
    expect(screen.getByText("Umbral mientras responde").nextSibling).toHaveTextContent("0.95");
    expect(screen.getByText("Pico mientras respondía").nextSibling).toHaveTextContent("0.91");
  });
});
