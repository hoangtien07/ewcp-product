// Spec-driven pane mapping — kernel GET /outcomes is the source of truth
// for slots, labels, and descriptions. This module holds the two things
// the wire does NOT carry (demo fixture names + sample intents) in one
// place, keyed by outcome_type, plus the fallback copy used when the
// kernel predates the registry endpoint.

import type { DemoFixtureName, OutcomeInputSpec, OutcomeSpecView } from "./api";

/** Kernel intake accepts at most ONE zip per request (its _collect_uploads
 * rejects a second zip field) — mirrored from InputReq.is_zip so the pane
 * can guard the pick instead of surfacing a 400. */
export function isZipInput(input: Pick<OutcomeInputSpec, "accept">): boolean {
  return input.accept
    .split(",")
    .map((a) => a.trim().toLowerCase())
    .includes(".zip");
}

/** Declared inputs across all specs, registry order, deduped by
 * multipart field name — the intake slots when no run is selected. */
export function unionInputs(specs: OutcomeSpecView[]): OutcomeInputSpec[] {
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
 * only ever wants that pack's slots); the union for a fresh request or
 * a pack run the registry doesn't know — the kernel's pack intake
 * accepts the union of ALL specs' input names, so the union is the
 * correct fallback there. The general lane alone gets NO slots: its
 * intake takes only the fixed `files` key + revision_note, and a pack
 * slot name would post a field the kernel general rejects (422). */
export function inputsFor(
  specs: OutcomeSpecView[],
  outcomeType?: string,
): OutcomeInputSpec[] {
  if (outcomeType === "general") return [];
  if (outcomeType === undefined) return unionInputs(specs);
  return (
    specs.find((s) => s.outcome_type === outcomeType)?.requires_inputs ??
    unionInputs(specs)
  );
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
  // three_way_match needs mst_doanh_nghiep in context (required_context)
  // — the kernel's extract_context parses it (and ky) out of the intent
  // text, so the sample intent carries the demo buyer MST 0300000001.
  three_way_match: [
    {
      label: "3 chiều",
      fixtures: {
        po_files: "three_way_po",
        invoices_zip: "three_way_invoices",
        receipts: "three_way_receipts",
      },
      intent:
        "Đối chiếu 3 chiều PO với phiếu nhận kho và hóa đơn kỳ 09/2025, MST 0300000001",
    },
    {
      label: "3 chiều (sạch)",
      fixtures: {
        po_files: "three_way_po_clean",
        invoices_zip: "three_way_invoices_clean",
        receipts: "three_way_receipts_clean",
      },
      intent:
        "Đối chiếu 3 chiều PO với phiếu nhận kho và hóa đơn kỳ 09/2025, MST 0300000001",
    },
    {
      label: "3 chiều (PO lỗi)",
      fixtures: {
        po_files: "three_way_po_corrupt",
        invoices_zip: "three_way_invoices",
        receipts: "three_way_receipts",
      },
      intent:
        "Đối chiếu 3 chiều PO với phiếu nhận kho và hóa đơn kỳ 09/2025, MST 0300000001",
    },
  ],
};

// Starter intents per outcome_type — the gallery chips that fill the
// intent box on click. The wire carries no example intents, so (like
// DEMO_PRESETS) this is a pane-side mapping bound to whatever the
// registry actually exposes. Entries must be routable by the kernel:
// packs with required_context need the parsed keys in the text (the
// three_way samples carry the demo buyer MST + kỳ).
export const STARTER_INTENTS: Record<string, string[]> = {
  invoice_recon: [
    "Kiểm tra hóa đơn đầu vào kỳ 09/2025 có khớp sổ kế toán không",
  ],
  dossier_check: ["Hồ sơ nhập khẩu lô hàng này đã đủ chứng từ chưa"],
  three_way_match: [
    "Kiểm tra hóa đơn nhà cung cấp khớp PO và phiếu nhận kho kỳ 09/2025, MST 0300000001",
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

/** Starter-intent chips for a spec's gallery card: the demo presets'
 * intents first (they're known to route — required_context is baked in),
 * then the curated starters, deduped and capped at 2. A pack with no
 * mapping renders no chips rather than a guess. */
export function starterIntentsFor(spec: OutcomeSpecView): string[] {
  const out: string[] = [];
  const push = (text: string) => {
    if (text && !out.includes(text) && out.length < 2) out.push(text);
  };
  for (const p of DEMO_PRESETS[spec.outcome_type] ?? []) push(p.intent);
  for (const t of STARTER_INTENTS[spec.outcome_type] ?? []) push(t);
  return out;
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
