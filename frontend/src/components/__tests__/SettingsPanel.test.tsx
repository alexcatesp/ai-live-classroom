import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SettingsPanel } from "../SettingsPanel";
import type { DeviceInventory, Settings, SettingsResponse } from "../../lib/types";

const SETTINGS: Settings = {
  realtime_model: "gpt-realtime",
  voice: "marin",
  input_device: null,
  output_device: null,
  wake_phrase: "Oye Chat",
  wake_sensitivity: 0.5,
  wake_refractory_seconds: 2,
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

function setup(data: SettingsResponse | null, devices: DeviceInventory | null = DEVICES) {
  const handlers = {
    onSave: vi.fn(),
    onSaveApiKey: vi.fn(),
    onClearApiKey: vi.fn(),
  };
  render(
    <SettingsPanel data={data} devices={devices} saving={false} error={null} {...handlers} />,
  );
  return handlers;
}

describe("SettingsPanel", () => {
  it("says when no key is stored on this computer", () => {
    setup({ settings: SETTINGS, api_key_configured: false });
    expect(screen.getByText(/No hay ninguna clave guardada/)).toBeInTheDocument();
  });

  it("says when a key is stored, without ever showing it", () => {
    setup({ settings: SETTINGS, api_key_configured: true });
    expect(screen.getByText(/clave guardada y cifrada/)).toBeInTheDocument();
    expect(screen.getByLabelText("Clave de la API")).toHaveValue("");
  });

  it("masks the key as it is typed", () => {
    setup({ settings: SETTINGS, api_key_configured: false });
    expect(screen.getByLabelText("Clave de la API")).toHaveAttribute("type", "password");
  });

  it("explains that the key does not travel with the portable folder (D-07)", () => {
    setup({ settings: SETTINGS, api_key_configured: true });
    expect(screen.getByText(/volver a introducirla/)).toBeInTheDocument();
  });

  it("will not save an empty key", () => {
    setup({ settings: SETTINGS, api_key_configured: false });
    expect(screen.getByRole("button", { name: "Guardar clave" })).toBeDisabled();
  });

  it("saves the key and clears the field afterwards", async () => {
    const handlers = setup({ settings: SETTINGS, api_key_configured: false });
    const input = screen.getByLabelText("Clave de la API");

    await userEvent.type(input, "sk-de-prueba");
    await userEvent.click(screen.getByRole("button", { name: "Guardar clave" }));

    expect(handlers.onSaveApiKey).toHaveBeenCalledWith("sk-de-prueba");
    expect(input).toHaveValue("");
  });

  it("offers to delete a stored key", async () => {
    const handlers = setup({ settings: SETTINGS, api_key_configured: true });
    await userEvent.click(screen.getByRole("button", { name: "Borrar" }));
    expect(handlers.onClearApiKey).toHaveBeenCalledOnce();
  });

  it("lists the devices Windows reports", () => {
    setup({ settings: SETTINGS, api_key_configured: false });
    expect(screen.getByLabelText("Micrófono")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Micrófono integrado" })).toBeInTheDocument();
  });

  it("saves the chosen microphone by name", async () => {
    const handlers = setup({ settings: SETTINGS, api_key_configured: false });
    await userEvent.selectOptions(screen.getByLabelText("Micrófono"), "Micrófono integrado");

    expect(handlers.onSave).toHaveBeenCalledWith(
      expect.objectContaining({ input_device: "Micrófono integrado" }),
    );
  });

  it("reports a broken audio stack instead of an empty list", () => {
    setup({ settings: SETTINGS, api_key_configured: false }, {
      inputs: [],
      outputs: [],
      error: "No se encontró la biblioteca de audio PortAudio.",
    });
    expect(screen.getByText(/PortAudio/)).toBeInTheDocument();
  });

  it("warns about the cost of raising the sensitivity (R-1)", () => {
    setup({ settings: SETTINGS, api_key_configured: false });
    expect(screen.getByText(/falsos positivos/)).toBeInTheDocument();
  });

  it("shows a loading state before the settings arrive", () => {
    setup(null);
    expect(screen.getByText("Cargando…")).toBeInTheDocument();
  });
});
