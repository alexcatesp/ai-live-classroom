import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { App } from "../App";

/**
 * Double-clicking the application and getting no window at all is the worst
 * failure this thing can have: nothing on screen, nothing to report, nothing
 * to search for. The shell now always opens the window and hands over the
 * reason; these tests keep that reason on screen.
 */
afterEach(() => {
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
