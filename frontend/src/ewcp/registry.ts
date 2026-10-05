// Spec-driven pane mapping — kernel GET /outcomes is the source of truth
// for slots, labels, and descriptions. This module holds the two things
// the wire does NOT carry (demo fixture names + sample intents) in one
// place, keyed by outcome_type, plus the fallback copy used when the
// kernel predates the registry endpoint.

import type {
  DemoFixtureName,
  OutcomeInputSpec,
  OutcomeSpecView,
} from "./api";

/** Kernel intake accepts at most ONE zip per request (its _collect_uploads
 * rejects a second zip field) — mirrored from InputReq.is_zip so the pane
 * can guard the pick instead of surfacing a 400. */
export function isZipInput(
  input: Pick<OutcomeInputSpec, "accept">,
): boolean {
  return input.accept
    .split(",")
    .map((a) => a.trim().toLowerCase())
    .includes(".zip");
}

/** Declared inputs across all specs, registry order, deduped by
 * multipart field name — the intake slots when no run is selected. */
export function unionInputs(
  specs: OutcomeSpecView[],
): OutcomeInputSpec[] {
  const seen = new Set<string>();
  const out: OutcomeInputSpec[] = [];
  for (const s of specs) {
    for (const i of s.requires_inputs) {
      if (!seen.has(i.name)) {
        seen.add(i.name);
        out.push(i);
      }
    }
  }
  return out;
}

/** Inputs the slot row should show: the active run's own spec (ask-back
 * only ever wants that pack's slots); the union for a fresh request or an
 * outcome type the registry doesn't know. */
export function inputsFor(
  specs: OutcomeSpecView[],
  outcomeType?: string,
): OutcomeInputSpec[] {
  const spec = outcomeType
    ? specs.find((s) => s.outcome_type === outcomeType)
    : undefined;
  return spec ? spec.requires_inputs : unionInputs(specs);
}

export interface DemoPreset {
  /** button text under "Dữ liệu mẫu:" */
  label: string;
  /** input field name -> kernel demo fixture (GET /demo/fixtures/{name}) */
  fixtures: Record<string, DemoFixtureName>;
  /** sample intent posted when the intent box is still empty — doubles
   * as the routing hint shown in the textarea placeholder */
  intent: string;
}

// Demo presets per outcome_type — the registry exposes no fixture names
// or route examples, so this is the single mapping point. A pack without
// an entry here simply gets no sample-data buttons.
export const DEMO_PRESETS: Record<string, DemoPreset[]> = {
  invoice_recon: [
    {
      label: "đối soát",
      fixtures: { invoices_zip: "invoices", books: "books" },
      intent: "Đối soát hóa đơn kỳ 09/2025",
    },
    {
      label: "đối soát (file lỗi)",
      fixtures: { invoices_zip: "invoices_corrupt", books: "books" },
      intent: "Đối soát hóa đơn kỳ 09/2025",
    },
  ],
  dossier_check: [
    {
      label: "hồ sơ chứng từ",
      fixtures: { dossier_zip: "dossier" },
      intent: "Kiểm tra hồ sơ chứng từ lô hàng gần nhất",
    },
  ],
};

export interface BoundPreset extends DemoPreset {
  outcomeType: string;
  /** the owning spec's declared input names — fixture entries naming a
   * field the spec doesn't declare are skipped (stale mapping guard) */
  inputs: string[];
}

/** Presets bound to the specs the registry actually exposes, registry
 * order — a registered pack drives buttons; an unregistered mapping is
 * dead config that never renders. */
export function bindPresets(specs: OutcomeSpecView[]): BoundPreset[] {
  return specs.flatMap((s) =>
    (DEMO_PRESETS[s.outcome_type] ?? []).map((p) => ({
      ...p,
      outcomeType: s.outcome_type,
      inputs: s.requires_inputs.map((i) => i.name),
    })),
  );
}

// Pre-registry fallback — kernels before the /outcomes endpoint (and any
// fetch failure) leave the pane on this static copy of the seeded packs
// rather than empty. Mirrored from the kernel's own OutcomeSpec rows
// (invoice_recon/spec.py, dossier_check/spec.py).
export const FALLBACK_SPECS: OutcomeSpecView[] = [
  {
    outcome_type: "dossier_check",
    description:
      "Kiểm tra hồ sơ chứng từ lô hàng: canonical docs -> check nhất quán chéo.",
    required_checks: ["doc_partition", "verdict_recompute", "traceability"],
    requires_inputs: [
      {
        name: "dossier_zip",
        accept: ".zip",
        label_vn: "Zip chứa chứng từ lô hàng (txt/pdf/xml)",
        required: true,
      },
    ],
  },
  {
    outcome_type: "invoice_recon",
    description:
      "Đối soát hóa đơn đầu vào 1 kỳ: canonical input -> trạng thái từng dòng.",
    required_checks: ["input_partition", "totals_match", "traceability"],
    requires_inputs: [
      {
        name: "invoices_zip",
        accept: ".zip",
        label_vn: "Zip chứa file hóa đơn (xml/pdf)",
        required: true,
      },
      {
        name: "books",
        accept: ".csv,.xlsx",
        label_vn: "Sổ kế toán kỳ này (csv/xlsx export)",
        required: true,
      },
    ],
  },
];
