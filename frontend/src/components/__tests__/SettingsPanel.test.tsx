import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SettingsPanel } from "../SettingsPanel";
import type { DeviceInventory, Settings, SettingsResponse } from "../../lib/types";

const SETTINGS: Settings = {
  realtime_model: "gpt-realtime-2",
  voice: "marin",
  input_device: null,
  output_device: null,
  wake_phrase: "Oye Chat",
  wake_sensitivity: 0.5,
  wake_refractory_seconds: 2,
  wake_vad_threshold: 0.5,
  wake_confirmation_frames: 2,
  echo_guard_margin: 0.15,
  turn_silence_ms: 2000,
  transcription_model: "gpt-4o-mini-transcribe",
  realtime_noise_reduction: "far_field",
  history_max_tokens: 4000,
  max_response_seconds: 45,
  materials_dir: null,
  transcript_retention: "discard",
  daily_cost_limit_eur: null,
  session_cost_limit_eur: null,
};

const DEVICES: DeviceInventory = {
  inputs: [{ index: 0, name: "Micrófono integrado", channels: 1, is_default: true }],
  outputs: [{ index: 1, name: "Altavoces", channels: 2, is_default: true }],
  error: null,
};

function stored(
  api_key_configured: boolean,
  extra: Partial<SettingsResponse> = {},
): SettingsResponse {
  return {
    settings: SETTINGS,
    api_key_configured,
    requires_passphrase: false,
    unlocked: true,
    ...extra,
  };
}

function setup(data: SettingsResponse | null, devices: DeviceInventory | null = DEVICES) {
  const handlers = {
    onSave: vi.fn(),
    onSaveApiKey: vi.fn(),
    onClearApiKey: vi.fn(),
    onUnlock: vi.fn(),
  };
  render(
    <SettingsPanel data={data} devices={devices} saving={false} error={null} {...handlers} />,
  );
  return handlers;
}

describe("SettingsPanel", () => {
  it("says when no key is stored on this computer", () => {
    setup(stored(false));
    expect(screen.getByText(/No hay ninguna clave guardada/)).toBeInTheDocument();
  });

  it("says when a key is stored, without ever showing it", () => {
    setup(stored(true));
    expect(screen.getByText(/clave guardada y cifrada/)).toBeInTheDocument();
    expect(screen.getByLabelText("Clave de la API")).toHaveValue("");
  });

  it("masks the key as it is typed", () => {
    setup(stored(false));
    expect(screen.getByLabelText("Clave de la API")).toHaveAttribute("type", "password");
  });

  it("explains that the key does not travel with the portable folder (D-07)", () => {
    setup(stored(true));
    expect(screen.getByText(/volver a introducirla/)).toBeInTheDocument();
  });

  it("will not save an empty key", () => {
    setup(stored(false));
    expect(screen.getByRole("button", { name: "Guardar clave" })).toBeDisabled();
  });

  it("saves the key and clears the field afterwards", async () => {
    const handlers = setup(stored(false));
    const input = screen.getByLabelText("Clave de la API");

    await userEvent.type(input, "sk-de-prueba");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));

    expect(handlers.onSaveApiKey).toHaveBeenCalledWith("sk-de-prueba", undefined);
    expect(input).toHaveValue("");
  });

  it("offers to delete a stored key", async () => {
    const handlers = setup(stored(true));
    await userEvent.click(screen.getByRole("button", { name: "Borrar" }));
    expect(handlers.onClearApiKey).toHaveBeenCalledOnce();
  });

  it("lists the devices Windows reports", () => {
    setup(stored(false));
    expect(screen.getByLabelText("Micrófono")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Micrófono integrado" })).toBeInTheDocument();
  });

  it("saves the chosen microphone by name", async () => {
    const handlers = setup(stored(false));
    await userEvent.selectOptions(screen.getByLabelText("Micrófono"), "Micrófono integrado");

    expect(handlers.onSave).toHaveBeenCalledWith(
      expect.objectContaining({ input_device: "Micrófono integrado" }),
    );
  });

  it("reports a broken audio stack instead of an empty list", () => {
    setup(stored(false), {
      inputs: [],
      outputs: [],
      error: "No se encontró la biblioteca de audio PortAudio.",
    });
    expect(screen.getByText(/PortAudio/)).toBeInTheDocument();
  });

  it("warns about the cost of raising the sensitivity (R-1)", () => {
    setup(stored(false));
    expect(screen.getByText(/falsos positivos/)).toBeInTheDocument();
  });

  it("shows a loading state before the settings arrive", () => {
    setup(null);
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
  });
});

// -- portable key: passphrase mode (risk R-5) ----------------------------

describe("SettingsPanel, clave portátil", () => {
  it("guarda la clave ligada al equipo si no se pide lo contrario", async () => {
    const handlers = setup(stored(false));

    await userEvent.type(screen.getByLabelText("Clave de la API"), "sk-local");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));

    expect(handlers.onSaveApiKey).toHaveBeenCalledWith("sk-local", undefined);
  });

  it("explica el coste de cada opción antes de elegir", async () => {
    setup(stored(false));
    expect(screen.getByText(/cuenta de Windows de este equipo/)).toBeInTheDocument();

    await userEvent.click(
      screen.getByLabelText("Poder usar esta clave en otros ordenadores"),
    );
    expect(screen.getByText(/si la olvidas/)).toBeInTheDocument();
  });

  it("pide la contraseña antes de dejar guardar una clave portátil", async () => {
    setup(stored(false));

    await userEvent.type(screen.getByLabelText("Clave de la API"), "sk-portatil");
    await userEvent.click(
      screen.getByLabelText("Poder usar esta clave en otros ordenadores"),
    );

    expect(screen.getByRole("button", { name: "Guardar clave" })).toBeDisabled();
  });

  it("envía la contraseña junto con la clave", async () => {
    const handlers = setup(stored(false));

    await userEvent.type(screen.getByLabelText("Clave de la API"), "sk-portatil");
    await userEvent.click(
      screen.getByLabelText("Poder usar esta clave en otros ordenadores"),
    );
    await userEvent.type(screen.getByLabelText("Contraseña para la clave"), "mi frase");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));

    expect(handlers.onSaveApiKey).toHaveBeenCalledWith("sk-portatil", "mi frase");
  });

  it("no deja la contraseña escrita en el formulario", async () => {
    setup(stored(false));

    await userEvent.type(screen.getByLabelText("Clave de la API"), "sk-portatil");
    await userEvent.click(
      screen.getByLabelText("Poder usar esta clave en otros ordenadores"),
    );
    const passphrase = screen.getByLabelText("Contraseña para la clave");
    await userEvent.type(passphrase, "mi frase");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));

    expect(passphrase).toHaveValue("");
  });

  it("dice que una clave portátil sirve en cualquier equipo", () => {
    setup(stored(true, { requires_passphrase: true }));
    expect(screen.getByText(/válida en cualquier equipo/)).toBeInTheDocument();
  });

  it("pide desbloquear cuando la clave está sellada", async () => {
    const handlers = setup(stored(true, { requires_passphrase: true, unlocked: false }));

    expect(screen.getByRole("alert")).toHaveTextContent("protegida con contraseña");

    await userEvent.type(screen.getByLabelText("Desbloquea la clave"), "mi frase");
    await userEvent.click(screen.getByRole("button", { name: "Desbloquear" }));

    expect(handlers.onUnlock).toHaveBeenCalledWith("mi frase");
  });

  it("desbloquea también al pulsar Intro", async () => {
    const handlers = setup(stored(true, { requires_passphrase: true, unlocked: false }));

    await userEvent.type(screen.getByLabelText("Desbloquea la clave"), "mi frase{Enter}");

    expect(handlers.onUnlock).toHaveBeenCalledWith("mi frase");
  });

  it("no pide desbloquear una clave ya abierta", () => {
    setup(stored(true, { requires_passphrase: true, unlocked: true }));
    expect(screen.queryByRole("button", { name: "Desbloquear" })).not.toBeInTheDocument();
  });
});

// -- defences against false positives and echo ---------------------------

describe("SettingsPanel, defensas del detector", () => {
  it("permite desactivar el filtro de voz para poder medir su efecto", async () => {
    const handlers = setup(stored(false));

    await userEvent.click(screen.getByLabelText("Activar solo cuando haya voz"));

    expect(handlers.onSave).toHaveBeenCalledWith(
      expect.objectContaining({ wake_vad_threshold: 0 }),
    );
  });

  it("permite desactivar la guarda de eco cuando se usan auriculares", async () => {
    const handlers = setup(stored(false));

    await userEvent.click(
      screen.getByLabelText("Evitar que el asistente se oiga a sí mismo"),
    );

    expect(handlers.onSave).toHaveBeenCalledWith(
      expect.objectContaining({ echo_guard_margin: 0 }),
    );
  });

  it("señala que la sensibilidad se mide, no se adivina", () => {
    setup(stored(false));
    expect(screen.getByText(/measure_wakeword/)).toBeInTheDocument();
  });
});
