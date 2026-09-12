/** The diagnostics screen (spec section 4.2). */

import type { CheckResult, DiagnosticsReport } from "../lib/types";

const STATUS_LABEL: Record<CheckResult["status"], string> = {
  ok: "Correcto",
  warning: "Aviso",
  failed: "Fallo",
  skipped: "No comprobado",
};

interface Props {
  report: DiagnosticsReport | null;
  running: boolean;
  error: string | null;
  onRun: () => void;
}

export function DiagnosticsPanel({ report, running, error, onRun }: Props) {
  return (
    <section className="panel" aria-label="Diagnóstico">
      <header className="panel-header">
        <h2>Diagnóstico</h2>
        <button type="button" onClick={onRun} disabled={running}>
          {running ? "Comprobando…" : "Comprobar equipo"}
        </button>
      </header>

      {error && (
        <p className="warning-line" role="alert">
          {error}
        </p>
      )}

      {!report && !running && (
        <p className="muted">
          Comprueba el micrófono, los altavoces y la conexión antes de empezar la clase.
        </p>
      )}

      {report && (
        <>
          <p className={report.ready_to_start ? "summary-ok" : "summary-failed"} role="status">
            {report.ready_to_start
              ? "El equipo está listo para dar clase."
              : "Hay comprobaciones que impiden empezar."}
          </p>
          <ul className="check-list">
            {report.results.map((result) => (
              <li key={result.id} className={`check check-${result.status}`}>
                <div className="check-head">
                  <span className="check-label">{result.label}</span>
                  <span className={`check-status status-${result.status}`}>
                    {STATUS_LABEL[result.status]}
                  </span>
                </div>
                <p className="check-detail">{result.detail}</p>
                {result.remedy && <p className="check-remedy">{result.remedy}</p>}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
