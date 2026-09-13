import { describe, expect, it } from "vitest";

import { CLASS_IS_RUNNING, STATE_PRESENTATION } from "../states";
import type { SessionState } from "../types";

const ALL_STATES: SessionState[] = [
  "IDLE",
  "PREPARING",
  "READY",
  "PASSIVE_LISTENING",
  "ACTIVATED",
  "CAPTURING_REQUEST",
  "THINKING",
  "SPEAKING",
  "INTERRUPTED",
  "PAUSED",
  "STOPPED",
  "ERROR",
];

describe("state presentation", () => {
  it("covers every state of the specification", () => {
    expect(Object.keys(STATE_PRESENTATION).sort()).toEqual([...ALL_STATES].sort());
  });

  it("gives every state a label and a description in Spanish", () => {
    for (const state of ALL_STATES) {
      const presentation = STATE_PRESENTATION[state];
      expect(presentation.label.length).toBeGreaterThan(0);
      expect(presentation.description.length).toBeGreaterThan(0);
    }
  });

  it("marks the microphone open only where the backend does", () => {
    // Must match MIC_ACTIVE_STATES in backend/src/aiclassroom/session/state.py.
    const open = ALL_STATES.filter((state) => STATE_PRESENTATION[state].microphoneOpen);
    expect(open.sort()).toEqual([
      "ACTIVATED",
      "CAPTURING_REQUEST",
      "INTERRUPTED",
      "PASSIVE_LISTENING",
      "SPEAKING",
      "THINKING",
    ]);
  });

  it("never shows an error or a pause as listening", () => {
    expect(STATE_PRESENTATION.ERROR.microphoneOpen).toBe(false);
    expect(STATE_PRESENTATION.PAUSED.microphoneOpen).toBe(false);
    expect(STATE_PRESENTATION.STOPPED.microphoneOpen).toBe(false);
  });

  it("treats every in-class state as running", () => {
    expect([...CLASS_IS_RUNNING].sort()).toEqual([
      "ACTIVATED",
      "CAPTURING_REQUEST",
      "INTERRUPTED",
      "PASSIVE_LISTENING",
      "SPEAKING",
      "THINKING",
    ]);
  });
});
