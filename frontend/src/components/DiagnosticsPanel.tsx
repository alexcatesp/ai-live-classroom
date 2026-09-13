/** The diagnostics screen (spec section 4.2). */

import { useEffect, useId, useState } from "react";

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
  // Collapsible like an FAQ entry: once the equipment has passed, the list is
  // noise above the class controls. It never hides a problem, though -- a
  // report that blocks the class opens it again.
  const [expanded, setExpanded] = useState(true);
  const contentId = useId();

  useEffect(() => {
    if (report && !report.ready_to_start) setExpanded(true);
  }, [report]);

  const run = () => {
    setExpanded(true);
    onRun();
  };

  return (
    <section className="panel" aria-label="Diagnóstico">
      <header className="panel-header">
        <h2>
          <button
            type="button"
            className="accordion-toggle"
            aria-expanded={expanded}
            aria-controls={contentId}
            onClick={() => setExpanded((open) => !open)}
          >
            <svg className="chevron" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
              <path
                d="M5 3l5 5-5 5"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            Diagnóstico
          </button>
        </h2>
        {!expanded && report && (
          <span className={report.ready_to_start ? "summary-ok small" : "summary-failed small"}>
            {report.ready_to_start ? "Equipo listo" : "Hay fallos"}
          </span>
        )}
        <button type="button" onClick={run} disabled={running}>
          {running ? "Comprobando…" : "Comprobar equipo"}
        </button>
      </header>

      <div id={contentId} hidden={!expanded}>
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
      </div>
    </section>
  );
}
