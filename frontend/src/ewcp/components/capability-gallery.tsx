"use client";

// CapabilityGallery — the governed-outcome catalog under the intent box.
// One card per outcome the registry reports — never a hard-coded pack
// list — so a newly registered pack appears by itself. Cards only
// pre-fill the draft; the free-form intent box stays the primary intake.

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
}: {
  specs: OutcomeSpecView[];
  busy: boolean;
  /** fills the intent box with the starter text */
  onIntent: (text: string) => void;
}) {
  const comingSoon = !specs.some(
    (s) => s.outcome_type === COMING_SOON.outcomeType,
  );
  return (
    <section className="space-y-2" aria-label="Gói nghiệp vụ governed">
      <h2 className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">
        Gói nghiệp vụ governed
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        {specs.map((s) => {
          const intents = starterIntentsFor(s);
          return (
            <article
              key={s.outcome_type}
              className="flex flex-col gap-2 rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900"
            >
              <div className="flex items-start justify-between gap-2">
                <h3 className="text-sm font-semibold">
                  {outcomeLabel(s.outcome_type)}
                </h3>
                <span className="shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">
                  Governed — niêm phong kiểm chứng
                </span>
              </div>
              <p className="text-xs text-zinc-500">{s.description}</p>
              {s.requires_inputs.length > 0 && (
                <ul className="flex flex-wrap gap-1">
                  {s.requires_inputs.map((i) => (
                    <li
                      key={i.name}
                      className="rounded bg-zinc-100 px-1.5 py-0.5 text-[11px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
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
                      className="rounded border border-blue-300 px-2 py-0.5 text-left text-[11px] text-blue-700 hover:bg-blue-50 disabled:opacity-50 dark:border-blue-700 dark:text-blue-300 dark:hover:bg-blue-950/40"
                    >
                      {t}
                    </button>
                  ))}
                </div>
              )}
            </article>
          );
        })}
        {comingSoon && (
          <div
            role="group"
            aria-disabled="true"
            className="flex cursor-not-allowed flex-col gap-2 rounded-lg border border-dashed border-zinc-300 bg-zinc-50 p-4 opacity-60 select-none dark:border-zinc-700 dark:bg-zinc-900/60"
          >
            <div className="flex items-start justify-between gap-2">
              <h3 className="text-sm font-semibold text-zinc-500 dark:text-zinc-400">
                {outcomeLabel(COMING_SOON.outcomeType)}
              </h3>
              <span className="shrink-0 rounded-full bg-zinc-200 px-2 py-0.5 text-[10px] font-semibold text-zinc-500 dark:bg-zinc-700 dark:text-zinc-400">
                Sắp có — chưa mở
              </span>
            </div>
            <p className="text-xs text-zinc-400">{COMING_SOON.description}</p>
          </div>
        )}
      </div>
    </section>
  );
}
