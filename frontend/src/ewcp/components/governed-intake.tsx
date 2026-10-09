"use client";

// GovernedIntake — the slot-aware intake shown when a capability card's
// "Chạy governed" is picked. One file field per spec input (accept +
// required from the wire); submit is gated on required slots and the
// kernel's one-zip-per-request rule (`_collect_uploads` rejects ≥2 zip
// fields — mirrored here so the pane fails the pick before the trip).

import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { OutcomeSpecView } from "@/ewcp/api";
import { outcomeLabel } from "@/ewcp/labels";
import { isZipInput } from "@/ewcp/registry";

export function GovernedIntake({
  spec,
  busy,
  onCancel,
  onLaunch,
}: {
  spec: OutcomeSpecView;
  busy: boolean;
  onCancel: () => void;
  /** slot name → picked files; the route forwards each file under its
   * spec slot name to kernel intake. */
  onLaunch: (slotFiles: Record<string, File[]>) => void;
}) {
  const [slots, setSlots] = useState<Record<string, File[]>>({});
  const missing = spec.requires_inputs.filter(
    (i) => i.required && (slots[i.name]?.length ?? 0) === 0,
  );
  const zipSlots = spec.requires_inputs.filter(
    (i) => isZipInput(i) && (slots[i.name]?.length ?? 0) > 0,
  );
  const canSubmit = !busy && missing.length === 0 && zipSlots.length <= 1;

  return (
    <section
      className="space-y-3 rounded-lg border border-emerald-300 bg-emerald-50/60 p-4 dark:border-emerald-800 dark:bg-emerald-950/30"
      aria-label={`Chạy governed: ${outcomeLabel(spec.outcome_type)}`}
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
      {missing.length > 0 && (
        <p className="text-xs text-amber-600">
          Còn thiếu đầu vào bắt buộc:{" "}
          {missing.map((i) => i.label_vn).join(", ")}.
        </p>
      )}
      <Button
        type="button"
        disabled={!canSubmit}
        onClick={() => onLaunch(slots)}
        className="bg-emerald-600 text-white hover:bg-emerald-700"
      >
        {busy ? "Đang chạy…" : "Chạy governed"}
      </Button>
    </section>
  );
}
