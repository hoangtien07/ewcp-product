import { describe, expect, test } from "@rstest/core";

import type { OutcomeSpecView } from "@/ewcp/api";
import {
  bindPresets,
  FALLBACK_SPECS,
  inputsFor,
  isZipInput,
  starterIntentsFor,
  unionInputs,
} from "@/ewcp/registry";

const recon: OutcomeSpecView = {
  outcome_type: "invoice_recon",
  description: "d",
  required_checks: [],
  requires_inputs: [
    { name: "invoices_zip", accept: ".zip", label_vn: "a", required: true },
    { name: "books", accept: ".csv,.xlsx", label_vn: "b", required: true },
  ],
};
const dossier: OutcomeSpecView = {
  outcome_type: "dossier_check",
  description: "d",
  required_checks: [],
  requires_inputs: [
    { name: "dossier_zip", accept: ".zip", label_vn: "c", required: true },
  ],
};
const threeWay: OutcomeSpecView = {
  outcome_type: "three_way_match",
  description: "d",
  required_checks: [],
  requires_inputs: [
    {
      name: "po_files",
      accept: ".pdf,.xlsx,.csv",
      label_vn: "a",
      required: true,
    },
    { name: "invoices_zip", accept: ".zip", label_vn: "b", required: true },
    {
      name: "receipts",
      accept: ".xlsx,.csv",
      label_vn: "c",
      required: false,
    },
  ],
};

describe("ewcp registry — spec-driven pane mapping", () => {
  test("isZipInput mirrors the kernel's InputReq.is_zip", () => {
    expect(isZipInput({ accept: ".zip" })).toBe(true);
    expect(isZipInput({ accept: ".csv,.zip" })).toBe(true);
    expect(isZipInput({ accept: ".csv,.xlsx" })).toBe(false);
  });

  test("unionInputs dedupes multipart field names, registry order", () => {
    const specs: OutcomeSpecView[] = [
      recon,
      dossier,
      {
        outcome_type: "other",
        description: "",
        required_checks: [],
        requires_inputs: [
          {
            name: "books", // same field name as recon's — deduped
            accept: ".csv",
            label_vn: "x",
            required: false,
          },
        ],
      },
    ];
    expect(unionInputs(specs).map((i) => i.name)).toEqual([
      "invoices_zip",
      "books",
      "dossier_zip",
    ]);
  });

  test("inputsFor narrows to the run's own spec, union when unknown", () => {
    const specs = [recon, dossier];
    expect(
      inputsFor(specs, "dossier_check").map((i) => i.name),
    ).toEqual(["dossier_zip"]);
    // fresh intake (no run selected) shows the union of all pack slots
    expect(inputsFor(specs).map((i) => i.name)).toHaveLength(3);
    // a pack run the registry doesn't know still gets the union — the
    // kernel's pack intake accepts every spec's input names, so the
    // slots post fields the kernel collects (e.g. a three_way run while
    // the registry fetch failed over to FALLBACK_SPECS)
    expect(inputsFor(specs, "unregistered").map((i) => i.name)).toHaveLength(
      3,
    );
    // the general lane is the ONLY empty case: kernel general intake
    // takes the fixed `files` key + revision_note — a spec slot name
    // posts a field the kernel rejects (422)
    expect(inputsFor(specs, "general")).toEqual([]);
  });

  test("bindPresets only exposes demos for registered packs", () => {
    const only = bindPresets([dossier]);
    expect(only).toHaveLength(1);
    expect(only[0]?.outcomeType).toBe("dossier_check");
    expect(only[0]?.fixtures).toEqual({ dossier_zip: "dossier" });
    // a pack with no mapping yields no buttons
    expect(
      bindPresets([
        {
          outcome_type: "unknown_pack",
          description: "",
          required_checks: [],
          requires_inputs: [],
        },
      ]),
    ).toHaveLength(0);
    // preset inputs track the spec's declared names
    expect(only[0]?.inputs).toEqual(["dossier_zip"]);
  });

  test("three_way_match presets map every declared slot to a kernel fixture", () => {
    const bound = bindPresets([threeWay]);
    // main batch (exception-rich), clean set, corrupt PO — mirroring the
    // kernel's /demo/fixtures/{name} allowlist
    expect(bound.map((p) => p.label)).toEqual([
      "3 chiều",
      "3 chiều (sạch)",
      "3 chiều (PO lỗi)",
    ]);
    for (const p of bound) {
      expect(p.outcomeType).toBe("three_way_match");
      expect(Object.keys(p.fixtures)).toEqual([
        "po_files",
        "invoices_zip",
        "receipts",
      ]);
      // required_context mst_doanh_nghiep is parsed out of the intent —
      // the sample must carry a buyer MST or intake stalls on clarify
      expect(p.intent).toContain("0300000001");
    }
    // fixture names are what the kernel serves — the wire carries no
    // fixture names so this is the only place the pairing is pinned
    expect(bound[0]?.fixtures).toEqual({
      po_files: "three_way_po",
      invoices_zip: "three_way_invoices",
      receipts: "three_way_receipts",
    });
  });

  test("starterIntentsFor caps at 2 deduped, preset intents first", () => {
    // invoice_recon has 2 presets sharing one intent -> that intent +
    // one curated starter = 2 chips
    const reconIntents = starterIntentsFor(recon);
    expect(reconIntents).toHaveLength(2);
    expect(reconIntents[0]).toContain("Đối soát hóa đơn");
    // three_way_match has 3 presets all on the same intent (with the
    // required_context MST baked in) -> dedupe leaves it first
    const threeWayIntents = starterIntentsFor(threeWay);
    expect(threeWayIntents).toHaveLength(2);
    for (const t of threeWayIntents) {
      expect(t).toContain("0300000001");
    }
    // a pack with no mapping renders no chips rather than a guess
    expect(
      starterIntentsFor({
        outcome_type: "unknown_pack",
        description: "",
        required_checks: [],
        requires_inputs: [],
      }),
    ).toEqual([]);
  });

  test("FALLBACK_SPECS mirrors the kernel's seeded packs (registry order)", () => {
    // kernel OutcomeRegistry.specs() sorts route-priority desc —
    // dossier_check (10) precedes invoice_recon (0)
    expect(FALLBACK_SPECS.map((s) => s.outcome_type)).toEqual([
      "dossier_check",
      "invoice_recon",
    ]);
    expect(
      FALLBACK_SPECS.flatMap((s) => s.requires_inputs.map((i) => i.name)),
    ).toEqual(["dossier_zip", "invoices_zip", "books"]);
  });
});
