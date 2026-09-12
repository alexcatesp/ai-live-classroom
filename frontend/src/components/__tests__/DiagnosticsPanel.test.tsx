import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DiagnosticsPanel } from "../DiagnosticsPanel";
import type { CheckResult, DiagnosticsReport } from "../../lib/types";

function check(overrides: Partial<CheckResult> = {}): CheckResult {
  return {
    id: "microphone",
    label: "Micrófono",
    status: "ok",
    detail: "1 dispositivo de entrada.",
    remedy: null,
    at: "2026-01-01T10:00:00Z",
    ...overrides,
  };
}

function report(overrides: Partial<DiagnosticsReport> = {}): DiagnosticsReport {
  return {
    status: "ok",
    ready_to_start: true,
    duration_seconds: 1.2,
    started_at: "2026-01-01T10:00:00Z",
    finished_at: "2026-01-01T10:00:01Z",
    results: [check()],
    ...overrides,
  };
}

describe("DiagnosticsPanel", () => {
  it("invites the teacher to check the equipment before class", () => {
    render(<DiagnosticsPanel report={null} running={false} error={null} onRun={() => {}} />);
    expect(screen.getByText(/antes de empezar la clase/)).toBeInTheDocument();
  });

  it("runs the diagnostics when asked", async () => {
    const onRun = vi.fn();
    render(<DiagnosticsPanel report={null} running={false} error={null} onRun={onRun} />);

    await userEvent.click(screen.getByRole("button", { name: "Comprobar equipo" }));
    expect(onRun).toHaveBeenCalledOnce();
  });

  it("disables the button while checking", () => {
    render(<DiagnosticsPanel report={null} running error={null} onRun={() => {}} />);
    expect(screen.getByRole("button", { name: "Comprobando…" })).toBeDisabled();
  });

  it("says plainly when the equipment is ready", () => {
    render(<DiagnosticsPanel report={report()} running={false} error={null} onRun={() => {}} />);
    expect(screen.getByRole("status")).toHaveTextContent("listo para dar clase");
  });

  it("says plainly when something blocks the class", () => {
    render(
      <DiagnosticsPanel
        report={report({ ready_to_start: false, status: "failed" })}
        running={false}
        error={null}
        onRun={() => {}}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("impiden empezar");
  });

  it("shows the remedy of a failed check, which is what the teacher acts on", () => {
    render(
      <DiagnosticsPanel
        report={report({
          ready_to_start: false,
          results: [
            check({
              status: "failed",
              detail: "Windows no ofrece ningún dispositivo de entrada.",
              remedy: "Conecta un micrófono y revisa los permisos de micrófono en Windows.",
            }),
          ],
        })}
        running={false}
        error={null}
        onRun={() => {}}
      />,
    );

    expect(screen.getByText(/Conecta un micrófono/)).toBeInTheDocument();
    expect(screen.getByText("Fallo")).toBeInTheDocument();
  });

  it("distinguishes a skipped check from a failed one", () => {
    render(
      <DiagnosticsPanel
        report={report({
          results: [check({ id: "tls", label: "Certificados TLS", status: "skipped" })],
        })}
        running={false}
        error={null}
        onRun={() => {}}
      />,
    );
    expect(screen.getByText("No comprobado")).toBeInTheDocument();
  });

  it("shows an error from the backend itself", () => {
    render(
      <DiagnosticsPanel
        report={null}
        running={false}
        error="No se pudo contactar con el motor local."
        onRun={() => {}}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("motor local");
  });
});
