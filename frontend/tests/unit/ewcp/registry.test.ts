import { describe, expect, test } from "@rstest/core";

import type { OutcomeSpecView } from "@/ewcp/api";
import {
  FALLBACK_SPECS,
  inputsFor,
  isZipInput,
  starterIntentsFor,
  unionInputs,
} from "@/ewcp/registry";

const a: OutcomeSpecView = {
  outcome_type: "invoice_recon",
  description: "x",
  required_checks: [],
  requires_inputs: [
    { name: "invoices_zip", accept: ".zip", label_vn: "z", required: true },
    { name: "books", accept: ".csv", label_vn: "b", required: true },
  ],
};
const b: OutcomeSpecView = {
  outcome_type: "dossier_check",
  description: "x",
  required_checks: [],
  requires_inputs: [
    { name: "invoices_zip", accept: ".zip", label_vn: "z", required: true },
    { name: "dossier_zip", accept: ".zip", label_vn: "d", required: true },
  ],
};

describe("registry", () => {
  test("isZipInput detects .zip in the accept list", () => {
    expect(isZipInput({ accept: ".zip" })).toBe(true);
    expect(isZipInput({ accept: ".csv, .ZIP" })).toBe(true);
    expect(isZipInput({ accept: ".xlsx" })).toBe(false);
  });

  test("unionInputs dedupes by field name, registry order", () => {
    expect(unionInputs([a, b]).map((i) => i.name)).toEqual([
      "invoices_zip",
      "books",
      "dossier_zip",
    ]);
  });

  test("inputsFor returns the spec's own slots or the union", () => {
    expect(inputsFor([a, b]).length).toBe(3);
    expect(inputsFor([a, b], "dossier_check").map((i) => i.name)).toEqual([
      "invoices_zip",
      "dossier_zip",
    ]);
    // undeclared outcome_type (e.g. general lane) → no slots
    expect(inputsFor([a, b], "general")).toEqual([]);
  });

  test("starterIntentsFor caps at 2 and returns [] unmapped", () => {
    expect(starterIntentsFor(a)[0]).toContain("hóa đơn");
    expect(starterIntentsFor({ ...a, outcome_type: "nope" })).toEqual([]);
  });

  test("FALLBACK_SPECS mirror the seeded packs", () => {
    expect(FALLBACK_SPECS.map((s) => s.outcome_type)).toContain(
      "invoice_recon",
    );
  });
});
