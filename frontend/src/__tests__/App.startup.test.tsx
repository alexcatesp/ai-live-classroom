import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";

/**
 * Double-clicking the application and getting no window at all is the worst
 * failure this thing can have: nothing on screen, nothing to report, nothing
 * to search for. The shell now always opens the window and hands over the
 * reason; these tests keep that reason on screen.
 */
// Rendering with a handshake present starts the real connection, which would
// otherwise reach for a port that is not there and reject after the test has
// finished -- an unhandled rejection that fails the run without failing any
// test. Both ways out are stubbed.
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("sin backend en la prueba")));
  vi.stubGlobal(
    "WebSocket",
    class {
      close() {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete window.__AICLASSROOM_BACKEND__;
  delete window.__AICLASSROOM_ERROR__;
});

describe("App sin motor local", () => {
  it("muestra el motivo concreto que dio el contenedor", () => {
    window.__AICLASSROOM_ERROR__ =
      "No se encontró el motor local (aiclassroom-backend.exe).";
    render(<App />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "No se encontró el motor local (aiclassroom-backend.exe).",
    );
  });

  it("dice dónde mirar: la carpeta, el antivirus y el registro", () => {
    window.__AICLASSROOM_ERROR__ = "cualquier fallo";
    render(<App />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/runtime\\backend/);
    expect(alert).toHaveTextContent(/antivirus/);
    expect(alert).toHaveTextContent(/arranque\.log/);
  });

  it("sigue explicándose aunque el contenedor no diera ningún motivo", () => {
    render(<App />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      /no ha recibido los datos de conexión/,
    );
  });

  it("no muestra el aviso cuando el motor sí arrancó", () => {
    window.__AICLASSROOM_BACKEND__ = { port: 1234, token: "t" };
    render(<App />);

    expect(screen.queryByText(/No se pudo arrancar el motor local/)).not.toBeInTheDocument();
  });
});

describe("App con el motor caído a media sesión", () => {
  it("no deja rechazos sin gestionar cuando la API falla", async () => {
    // The interface fires its refresh without awaiting it, so a rejection here
    // used to surface as an unhandled promise rejection in the application
    // itself -- not just in a test. The socket's reconnect loop is what
    // recovers; this only has to stay quiet.
    const rejections: unknown[] = [];
    const capture = (event: PromiseRejectionEvent) => rejections.push(event.reason);
    window.addEventListener("unhandledrejection", capture);

    window.__AICLASSROOM_BACKEND__ = { port: 1234, token: "t" };
    render(<App />);
    await new Promise((resolve) => setTimeout(resolve, 50));

    window.removeEventListener("unhandledrejection", capture);
    expect(rejections).toEqual([]);
  });
});
