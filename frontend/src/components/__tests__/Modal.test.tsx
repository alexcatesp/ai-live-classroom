import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Modal } from "../Modal";

describe("Modal", () => {
  it("no pinta nada mientras está cerrado", () => {
    render(
      <Modal title="Configuración" open={false} onClose={() => {}}>
        contenido
      </Modal>,
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("se anuncia como diálogo con su título", () => {
    render(
      <Modal title="Configuración" open onClose={() => {}}>
        contenido
      </Modal>,
    );
    expect(screen.getByRole("dialog", { name: "Configuración" })).toHaveTextContent("contenido");
  });

  it("se cierra con el botón, con Escape y al pulsar fuera", async () => {
    const onClose = vi.fn();
    render(
      <Modal title="Configuración" open onClose={onClose}>
        contenido
      </Modal>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Cerrar" }));
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("dialog").parentElement!);
    expect(onClose).toHaveBeenCalledTimes(3);
  });

  it("no se cierra al pulsar dentro", async () => {
    const onClose = vi.fn();
    render(
      <Modal title="Configuración" open onClose={onClose}>
        <p>contenido</p>
      </Modal>,
    );
    await userEvent.click(screen.getByText("contenido"));
    expect(onClose).not.toHaveBeenCalled();
  });
});
