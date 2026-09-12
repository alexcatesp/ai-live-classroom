import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ClassControls } from "../ClassControls";
import type { SessionState } from "../../lib/types";

function setup(state: SessionState, overrides: Partial<Parameters<typeof ClassControls>[0]> = {}) {
  const handlers = {
    onPrepare: vi.fn(),
    onStart: vi.fn(),
    onPause: vi.fn(),
    onResume: vi.fn(),
    onStop: vi.fn(),
    onRecover: vi.fn(),
  };
  render(
    <ClassControls state={state} busy={false} readyToStart {...handlers} {...overrides} />,
  );
  return handlers;
}

describe("ClassControls", () => {
  it("offers to prepare a session when there is none", async () => {
    const handlers = setup("IDLE");
    await userEvent.click(screen.getByRole("button", { name: "Preparar sesión" }));
    expect(handlers.onPrepare).toHaveBeenCalledOnce();
  });

  it("offers a single start button once the session is ready (spec section 3)", async () => {
    const handlers = setup("READY");
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Iniciar clase" }));
    expect(handlers.onStart).toHaveBeenCalledOnce();
  });

  it("will not start a class the diagnostics have not cleared", () => {
    setup("READY", { readyToStart: false });
    expect(screen.getByRole("button", { name: "Iniciar clase" })).toBeDisabled();
    expect(screen.getByText(/Ejecuta el diagnóstico/)).toBeInTheDocument();
  });

  it("offers pause and finish while the class runs", () => {
    setup("PASSIVE_LISTENING");
    expect(screen.getByRole("button", { name: "Pausar" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Finalizar clase" })).toBeEnabled();
  });

  it("offers pause while the assistant is answering", () => {
    setup("SPEAKING");
    expect(screen.getByRole("button", { name: "Pausar" })).toBeInTheDocument();
  });

  it("offers to resume a paused class", async () => {
    const handlers = setup("PAUSED");
    await userEvent.click(screen.getByRole("button", { name: "Reanudar" }));
    expect(handlers.onResume).toHaveBeenCalledOnce();
  });

  it("offers to retry after an error", async () => {
    const handlers = setup("ERROR");
    await userEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(handlers.onRecover).toHaveBeenCalledOnce();
  });

  it("offers to prepare again once the class has finished", () => {
    setup("STOPPED");
    expect(screen.getByRole("button", { name: "Preparar sesión" })).toBeInTheDocument();
  });

  it("disables every control while a command is in flight", () => {
    setup("PASSIVE_LISTENING", { busy: true });
    for (const button of screen.getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
  });

  it("renders nothing before the state is known", () => {
    const { container } = render(
      <ClassControls
        state={null}
        busy={false}
        readyToStart
        onPrepare={() => {}}
        onStart={() => {}}
        onPause={() => {}}
        onResume={() => {}}
        onStop={() => {}}
        onRecover={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
