"use client";

// GovernedIntake — the slot-aware intake shown when a capability card's
// "Chạy governed" is picked. One file field per spec input (accept +
// required from the wire); submit is gated on required slots and the
// kernel's one-zip-per-request rule (`_collect_uploads` rejects ≥2 zip
// fields — mirrored here so the pane fails the pick before the trip).
//
// Context inputs (F2): the kernel 422s `missing context fields` when a
// required_context key (or a required context_schema field) arrives
// empty. `required_context` keys without a ContextField render as
// required text inputs labelled by the key name; schema fields carry
// their own label_vn/type/default. Values go back through `onLaunch`
// as plain form fields the route forwards to kernel context.

import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { OutcomeContextField, OutcomeSpecView } from "@/ewcp/api";
import { outcomeLabel } from "@/ewcp/labels";
import { isZipInput } from "@/ewcp/registry";

/** Merge `required_context` ∪ `context_schema` into the form field
 * list: schema fields keep their declared type/label/required; a key
 * named only in `required_context` becomes a required string input
 * labelled by the key (kernel wire carries no label for those). */
export function contextFieldsFor(
  spec: Pick<OutcomeSpecView, "required_context" | "context_schema">,
): OutcomeContextField[] {
  const byName = new Map<string, OutcomeContextField>();
  for (const name of spec.required_context) {
    byName.set(name, { name, type: "str", required: true, label_vn: name });
  }
  for (const f of spec.context_schema) {
    const bare = byName.get(f.name);
    byName.set(f.name, {
      ...f,
      required: f.required ?? bare?.required ?? false,
      label_vn: f.label_vn ?? bare?.label_vn ?? f.name,
    });
  }
  return [...byName.values()];
}

export function GovernedIntake({
  spec,
  busy,
  onCancel,
  onLaunch,
}: {
  spec: OutcomeSpecView;
  busy: boolean;
  onCancel: () => void;
  /** slot name → picked files; context key → entered value. The route
   * forwards each file under its spec slot name and each value under
   * its context key to kernel intake. */
  onLaunch: (
    slotFiles: Record<string, File[]>,
    contextValues: Record<string, string>,
  ) => void;
}) {
  const ctxFields = contextFieldsFor(spec);
  const [slots, setSlots] = useState<Record<string, File[]>>({});
  const [ctx, setCtx] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      ctxFields
        .filter((f) => f.default !== undefined && f.default !== null)
        .map((f) => [f.name, String(f.default)]),
    ),
  );
  const missing = spec.requires_inputs.filter(
    (i) => i.required && (slots[i.name]?.length ?? 0) === 0,
  );
  const missingCtx = ctxFields.filter(
    (f) => f.required === true && !(ctx[f.name] ?? "").trim(),
  );
  const zipSlots = spec.requires_inputs.filter(
    (i) => isZipInput(i) && (slots[i.name]?.length ?? 0) > 0,
  );
  const canSubmit =
    !busy &&
    missing.length === 0 &&
    missingCtx.length === 0 &&
    zipSlots.length <= 1;

  return (
    <section
      className="space-y-3 rounded-lg border border-emerald-300 bg-emerald-50/60 p-4 dark:border-emerald-800 dark:bg-emerald-950/30"
      aria-label={`Chạy kiểm chứng: ${outcomeLabel(spec.outcome_type)}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold">
            {outcomeLabel(spec.outcome_type)}
          </h3>
          <p className="text-muted-foreground text-xs">{spec.description}</p>
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onCancel}
          disabled={busy}
          className="h-7 shrink-0 text-[11px]"
        >
          Huỷ
        </Button>
      </div>
      {ctxFields.map((f) => (
        <div key={f.name} className="space-y-1">
          <label className="block text-xs font-medium" htmlFor={f.name}>
            {f.label_vn ?? f.name}
            {f.required ? "" : " (tuỳ chọn)"}
          </label>
          <input
            id={f.name}
            type="text"
            value={ctx[f.name] ?? ""}
            onChange={(e) =>
              setCtx((s) => ({ ...s, [f.name]: e.target.value }))
            }
            className="border-input bg-background w-full rounded border px-2 py-1 text-xs"
          />
        </div>
      ))}
      {spec.requires_inputs.map((input) => (
        <div key={input.name} className="space-y-1">
          <label className="block text-xs font-medium">
            {input.label_vn}
            {input.required ? "" : " (tuỳ chọn)"}
          </label>
          <input
            type="file"
            accept={input.accept}
            multiple
            onChange={(e) =>
              setSlots((s) => ({
                ...s,
                [input.name]: Array.from(e.target.files ?? []),
              }))
            }
            className="text-muted-foreground file:border-border file:bg-background block w-full text-xs file:mr-2 file:rounded file:border file:px-2 file:py-1 file:text-xs"
          />
          {(slots[input.name]?.length ?? 0) > 0 && (
            <p className="text-muted-foreground text-[11px]">
              {slots[input.name]!.map((f) => f.name).join(", ")}
            </p>
          )}
        </div>
      ))}
      {zipSlots.length > 1 && (
        <p className="text-destructive text-xs">
          Kernel chỉ nhận một trường zip mỗi lần chạy — bỏ bớt một trong{" "}
          {zipSlots.map((i) => i.name).join(", ")}.
        </p>
      )}
      {(missing.length > 0 || missingCtx.length > 0) && (
        <p className="text-xs text-amber-600">
          Còn thiếu đầu vào bắt buộc:{" "}
          {[
            ...missing.map((i) => i.label_vn),
            ...missingCtx.map((f) => f.label_vn ?? f.name),
          ].join(", ")}
          .
        </p>
      )}
      <Button
        type="button"
        disabled={!canSubmit}
        onClick={() => onLaunch(slots, ctx)}
        className="bg-emerald-600 text-white hover:bg-emerald-700"
      >
        {busy ? "Đang chạy…" : "Chạy kiểm chứng"}
      </Button>
    </section>
  );
}
