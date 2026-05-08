/**
 * GateCellInput.tsx — Single editable gate cell for the 5×3 grid.
 * ADMIN-UI-001 | Chunk 3
 *
 * Renders differently based on gate type:
 *   - TOGGLE_GATES  → checkbox toggle (1 = enabled, 0 = disabled)
 *   - DOLLAR_GATES  → number input with $ prefix, comma-formatted display
 *   - MS_GATES      → number input showing value in seconds (stored as ms)
 *   - default       → plain number input
 *
 * SaveStatus badge: idle (no badge) | saving (spinner) | saved (✓ green) | error (✗ red)
 *
 * saveError: when provided, renders a human-readable error message from the
 * last failed PATCH so the admin knows *why* the save failed, not just that
 * it did. The draft is reset to the last confirmed server value on error
 * so there is no ambiguity about what value is actually in the DB.
 *
 * 428 market-hours flow:
 *   When status === "market_confirm" an amber inline banner replaces the
 *   normal save controls, showing the pending new value and two buttons:
 *   Confirm (re-sends with confirm_market_hours: true) and Cancel (discards).
 */
"use client";
import React, { useState, useEffect } from "react";
import {
  A,
  SaveBtn,
} from "./_shared";
import {
  MS_GATES,
  TOGGLE_GATES,
  DOLLAR_GATES,
} from "@/types/gates";
import type { GateRow, SaveStatus } from "@/types/gates";

export interface GateCellInputProps {
  row:            GateRow;
  status:         SaveStatus;
  /** Human-readable error message from the last failed PATCH, if any. */
  saveError?:     string;
  onSave:         (newValue: number, reason: string | null) => void;
  /** Called when admin confirms a 428-gated save during market hours. */
  onConfirm?:     () => void;
  /** Called when admin cancels a 428-gated save. */
  onCancelConfirm?: () => void;
}

/** Convert stored value → display string */
function toDisplay(gateName: string, stored: number): string {
  if (MS_GATES.has(gateName)) return String(stored / 1000);
  return String(stored);
}

/** Convert display string → stored number (returns NaN on bad input) */
function fromDisplay(gateName: string, display: string): number {
  const n = parseFloat(display);
  if (isNaN(n)) return NaN;
  return MS_GATES.has(gateName) ? n * 1000 : n;
}

export function GateCellInput({
  row,
  status,
  saveError,
  onSave,
  onConfirm,
  onCancelConfirm,
}: GateCellInputProps) {
  const isToggle = TOGGLE_GATES.has(row.gate_name);
  const isDollar = DOLLAR_GATES.has(row.gate_name);
  const isMs     = MS_GATES.has(row.gate_name);

  const [draft, setDraft]       = useState<string>(toDisplay(row.gate_name, row.value));
  const [validErr, setValidErr] = useState<string>("");
  const [reason, setReason]     = useState<string>("");

  // Sync draft when server value updates (e.g. after 30s poll or successful save)
  useEffect(() => {
    setDraft(toDisplay(row.gate_name, row.value));
    setValidErr("");
  }, [row.value, row.gate_name]);

  // On error: reset draft to last confirmed server value so the admin
  // can see clearly what is actually in the DB vs what they tried to save.
  useEffect(() => {
    if (status === "error") {
      setDraft(toDisplay(row.gate_name, row.value));
      setValidErr("");
    }
  }, [status, row.value, row.gate_name]);

  const storedDraft = fromDisplay(row.gate_name, draft);
  const dirty = !isNaN(storedDraft) && storedDraft !== row.value;

  function validate(displayVal: string): string {
    const n = fromDisplay(row.gate_name, displayVal);
    if (isNaN(n)) return "Must be a number";
    if (n < row.min_value) return `Min ${row.min_value}`;
    if (n > row.max_value) return `Max ${row.max_value}`;
    return "";
  }

  function handleChange(v: string) {
    setDraft(v);
    setValidErr(validate(v));
  }

  function handleToggle(checked: boolean) {
    const newVal = checked ? 1 : 0;
    onSave(newVal, null);
  }

  function handleSave() {
    const err = validate(draft);
    if (err) { setValidErr(err); return; }
    onSave(storedDraft, reason.trim() || null);
    setReason("");
  }

  // ── Market-hours confirmation banner (428 flow) ──────────────
  // Rendered instead of normal controls while waiting for admin
  // to confirm or cancel a live-market gate edit.
  if (status === "market_confirm") {
    const pendingDisplay = isDollar
      ? `$${storedDraft.toLocaleString()}`
      : isMs
      ? `${storedDraft / 1000}s`
      : String(storedDraft);

    return (
      <div
        data-testid={`cell-${row.gate_name}-${row.tier}`}
        className="flex flex-col gap-1.5"
        style={{
          background: "rgba(217,119,6,0.08)",
          border:     `1px solid rgba(217,119,6,0.35)`,
          borderRadius: "6px",
          padding: "6px 8px",
        }}
      >
        <p
          className="text-xs font-mono font-semibold"
          style={{ color: "#d97706" }}
        >
          ⚠ Market is open
        </p>
        <p className="text-xs font-mono" style={{ color: A.muted }}>
          Save <span style={{ color: A.text, fontWeight: 600 }}>{pendingDisplay}</span> during live
          trading?
        </p>
        <div className="flex gap-1.5">
          <button
            data-testid={`confirm-btn-${row.gate_name}-${row.tier}`}
            onClick={onConfirm}
            className="px-2 py-0.5 rounded text-xs font-mono font-semibold"
            style={{
              background: "rgba(217,119,6,0.18)",
              border:     `1px solid rgba(217,119,6,0.5)`,
              color:      "#d97706",
              cursor:     "pointer",
            }}
          >
            Confirm
          </button>
          <button
            data-testid={`cancel-btn-${row.gate_name}-${row.tier}`}
            onClick={onCancelConfirm}
            className="px-2 py-0.5 rounded text-xs font-mono"
            style={{
              background: A.bg,
              border:     `1px solid ${A.border}`,
              color:      A.muted,
              cursor:     "pointer",
            }}
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  // ── Toggle gate ──────────────────────────────────────────────
  if (isToggle) {
    const isOn = row.value === 1;
    return (
      <div
        data-testid={`cell-${row.gate_name}-${row.tier}`}
        className="flex flex-col gap-1"
      >
        <div className="flex items-center gap-2">
          <button
            role="switch"
            aria-checked={isOn}
            aria-label={`Toggle ${row.gate_name} tier ${row.tier}`}
            onClick={() => handleToggle(!isOn)}
            disabled={status === "saving"}
            className="w-10 h-5 rounded-full relative transition-colors"
            style={{
              background: isOn ? A.green : A.faint,
              border: `1px solid ${isOn ? A.greenBorder : A.border}`,
              cursor: status === "saving" ? "not-allowed" : "pointer",
            }}
          >
            <span
              className="absolute top-0.5 w-4 h-4 rounded-full transition-all"
              style={{
                background: A.text,
                left: isOn ? "20px" : "2px",
              }}
            />
          </button>
          <SaveStatusBadge status={status} />
        </div>
        {status === "error" && saveError && (
          <p
            data-testid={`save-err-${row.gate_name}-${row.tier}`}
            className="text-xs font-mono"
            style={{ color: A.red }}
          >
            {saveError}
          </p>
        )}
      </div>
    );
  }

  // ── Number / Dollar / Ms gate ────────────────────────────────
  const placeholder = isDollar ? "$0" : isMs ? "0s" : "0";
  const prefix      = isDollar ? "$" : "";
  const suffix      = isMs ? "s" : "";

  return (
    <div
      data-testid={`cell-${row.gate_name}-${row.tier}`}
      className="flex flex-col gap-1"
    >
      <div className="flex items-center gap-1.5">
        {prefix && (
          <span className="text-xs font-mono" style={{ color: A.muted }}>{prefix}</span>
        )}
        <input
          type="number"
          aria-label={`${row.gate_name} tier ${row.tier}`}
          value={draft}
          placeholder={placeholder}
          min={row.min_value}
          max={row.max_value}
          step={isMs ? 0.1 : 1}
          onChange={e => handleChange(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") handleSave(); }}
          disabled={status === "saving"}
          className="w-24 px-2 py-1 rounded text-xs font-mono tabular-nums"
          style={{
            background: A.bg,
            border: `1px solid ${
              validErr ? A.redBorder : dirty ? A.amberBorder : A.border
            }`,
            color:   A.text,
            outline: "none",
          }}
        />
        {suffix && (
          <span className="text-xs font-mono" style={{ color: A.muted }}>{suffix}</span>
        )}
        <SaveBtn
          onClick={handleSave}
          saving={status === "saving"}
          saved={status === "saved"}
          dirty={dirty && !validErr}
        />
        <SaveStatusBadge status={status} />
      </div>

      {validErr && (
        <p
          data-testid={`err-${row.gate_name}-${row.tier}`}
          className="text-xs font-mono"
          style={{ color: A.red }}
        >
          {validErr}
        </p>
      )}

      {status === "error" && saveError && (
        <p
          data-testid={`save-err-${row.gate_name}-${row.tier}`}
          className="text-xs font-mono"
          style={{ color: A.red }}
        >
          {saveError}
        </p>
      )}

      {dirty && !validErr && (
        <input
          aria-label={`Reason for changing ${row.gate_name} tier ${row.tier}`}
          type="text"
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="Reason (optional)"
          className="px-2 py-0.5 rounded text-xs font-mono"
          style={{
            background: A.bg,
            border: `1px solid ${A.border}`,
            color: A.muted,
            outline: "none",
            width: "100%",
            maxWidth: "200px",
          }}
        />
      )}
    </div>
  );
}

function SaveStatusBadge({ status }: { status: SaveStatus }) {
  if (status === "saved") {
    return (
      <span data-testid="badge-saved" className="text-xs font-mono" style={{ color: A.green }}>
        ✓
      </span>
    );
  }
  if (status === "error") {
    return (
      <span data-testid="badge-error" className="text-xs font-mono" style={{ color: A.red }}>
        ✗
      </span>
    );
  }
  if (status === "saving") {
    return (
      <span data-testid="badge-saving" className="text-xs font-mono animate-pulse" style={{ color: A.muted }}>
        …
      </span>
    );
  }
  return null;
}
