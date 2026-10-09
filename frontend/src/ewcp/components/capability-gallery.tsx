"use client";

// CapabilityGallery — the governed-outcome catalog under the intent box.
// One card per outcome the registry reports — never a hard-coded pack
// list — so a newly registered pack appears by itself. Cards only
// pre-fill the draft; the free-form intent box stays the primary intake.

import { Button } from "@/components/ui/button";
import type { OutcomeSpecView } from "@/ewcp/api";
import { outcomeLabel } from "@/ewcp/labels";
import { starterIntentsFor } from "@/ewcp/registry";

// Announced-roadmap pack with no registered spec yet — greyed, disabled,
// non-clickable. Once the kernel registers it the registry card replaces
// this placeholder, keeping exactly one card for the pack.
const COMING_SOON = {
  outcomeType: "bank_recon",
  description:
    "Đối chiếu sao kê ngân hàng với sổ tiền gửi — pack tiếp theo trên roadmap.",
} as const;

export function CapabilityGallery({
  specs,
  busy,
  onIntent,
  onGoverned,
}: {
  specs: OutcomeSpecView[];
  busy: boolean;
  /** fills the intent box with the starter text */
  onIntent: (text: string) => void;
  /** picks the card's pack for governed intake — the slot-aware panel
   * opens under the intent box (A6: governed runs must be creatable
   * from the UI, not curl-only). */
  onGoverned: (spec: OutcomeSpecView) => void;
}) {
  const comingSoon = !specs.some(
    (s) => s.outcome_type === COMING_SOON.outcomeType,
  );
  return (
    <section className="space-y-2" aria-label="Gói nghiệp vụ governed">
      <h2 className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
        Gói nghiệp vụ governed
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {specs.map((s) => {
          const intents = starterIntentsFor(s);
          return (
            <article
              key={s.outcome_type}
              className="border-border bg-card flex flex-col gap-2 rounded-lg border p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {outcomeLabel(s.outcome_type)}
                </h3>
                <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                  Governed — niêm phong kiểm chứng
                </span>
              </div>
              <p className="text-muted-foreground text-xs">{s.description}</p>
              {s.requires_inputs.length > 0 && (
                <ul className="flex flex-wrap gap-1">
                  {s.requires_inputs.map((i) => (
                    <li
                      key={i.name}
                      className="bg-muted text-muted-foreground rounded px-1.5 py-0.5 text-[11px]"
                    >
                      {i.label_vn}
                      {i.required ? "" : " (tuỳ chọn)"}
                    </li>
                  ))}
                </ul>
              )}
              {intents.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {intents.map((t) => (
                    <button
                      key={t}
                      type="button"
                      disabled={busy}
                      onClick={() => onIntent(t)}
                      title="Điền vào ô yêu cầu"
                      className="border-border text-primary hover:bg-accent rounded border px-2 py-0.5 text-left text-[11px] transition-colors disabled:opacity-50"
                    >
                      {t}
                    </button>
                  ))}
                </div>
              )}
              <div className="mt-auto pt-1">
                <Button
                  type="button"
                  size="sm"
                  disabled={busy}
                  onClick={() => onGoverned(s)}
                  className="bg-emerald-600 text-white hover:bg-emerald-700"
                >
                  Chạy governed
                </Button>
              </div>
            </article>
          );
        })}
        {comingSoon && (
          <div
            role="group"
            aria-disabled="true"
            className="border-border bg-muted flex cursor-not-allowed flex-col gap-2 rounded-lg border border-dashed p-4 opacity-60 select-none"
          >
            <div className="flex items-start justify-between gap-2">
              <h3 className="text-muted-foreground text-sm font-semibold">
                {outcomeLabel(COMING_SOON.outcomeType)}
              </h3>
              <span className="bg-muted text-muted-foreground shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold">
                Sắp có — chưa mở
              </span>
            </div>
            <p className="text-muted-foreground text-xs">
              {COMING_SOON.description}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
