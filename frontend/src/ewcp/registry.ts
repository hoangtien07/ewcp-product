// Spec-driven pane mapping — GET /api/ewcp/outcomes is the source of
// truth for slots, labels, and descriptions. This module holds what the
// wire does NOT carry (starter intents) plus the fallback copy used
// when the kernel predates the registry endpoint. Demo-fixture presets
// from the legacy pane are dropped: no fixture proxy route exists on
// the product surface (kernel /demo/* stays kernel-side).

import type { OutcomeInputSpec, OutcomeSpecView } from "./api";

/** Kernel intake accepts at most ONE zip per request — mirrored from
 * InputReq.is_zip so the pane can guard the pick. */
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

/** Inputs the slot row should show: the active run's own spec; the union
 * for a fresh request. A run whose outcome_type the registry doesn't
 * declare gets NO slots — unioning pack slots there would offer fields
 * the intake rejects. */
export function inputsFor(
  specs: OutcomeSpecView[],
  outcomeType?: string,
): OutcomeInputSpec[] {
  if (outcomeType === undefined) return unionInputs(specs);
  return (
    specs.find((s) => s.outcome_type === outcomeType)?.requires_inputs ?? []
  );
}

// Starter intents per outcome_type — the gallery chips that fill the
// intent box on click. Entries must be routable by the kernel: packs
// with required_context need the parsed keys in the text (the three_way
// samples carry the demo buyer MST + kỳ).
export const STARTER_INTENTS: Record<string, string[]> = {
  invoice_recon: [
    "Kiểm tra hóa đơn đầu vào kỳ 09/2025 có khớp sổ kế toán không",
  ],
  dossier_check: ["Hồ sơ nhập khẩu lô hàng này đã đủ chứng từ chưa"],
  three_way_match: [
    "Kiểm tra hóa đơn nhà cung cấp khớp PO và phiếu nhận kho kỳ 09/2025, MST 0300000001",
  ],
};

/** Starter-intent chips for a spec's gallery card, capped at 2. A pack
 * with no mapping renders no chips rather than a guess. */
export function starterIntentsFor(spec: OutcomeSpecView): string[] {
  return (STARTER_INTENTS[spec.outcome_type] ?? []).slice(0, 2);
}

// Pre-registry fallback — kernels before /outcomes (and fetch failures)
// leave the pane on this static copy of the seeded packs.
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
