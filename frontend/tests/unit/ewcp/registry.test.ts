import { describe, expect, test } from "@rstest/core";

import type { OutcomeSpecView } from "@/ewcp/api";
import { FALLBACK_SPECS, isZipInput, starterIntentsFor } from "@/ewcp/registry";

const a: OutcomeSpecView = {
  outcome_type: "invoice_recon",
  description: "x",
  required_checks: [],
  requires_inputs: [
    { name: "invoices_zip", accept: ".zip", label_vn: "z", required: true },
    { name: "books", accept: ".csv", label_vn: "b", required: true },
  ],
  required_context: [],
  context_schema: [],
};
describe("registry", () => {
  test("isZipInput detects .zip in the accept list", () => {
    expect(isZipInput({ accept: ".zip" })).toBe(true);
    expect(isZipInput({ accept: ".csv, .ZIP" })).toBe(true);
    expect(isZipInput({ accept: ".xlsx" })).toBe(false);
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
