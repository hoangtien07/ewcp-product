import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { OutcomeSpecView } from "@/ewcp/api";
import { CapabilityGallery } from "@/ewcp/components/capability-gallery";
import { FALLBACK_SPECS } from "@/ewcp/registry";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const recon: OutcomeSpecView = {
  outcome_type: "invoice_recon",
  description: "Đối soát hóa đơn đầu vào 1 kỳ",
  required_checks: [],
  requires_inputs: [
    {
      name: "invoices_zip",
      accept: ".zip",
      label_vn: "Zip hóa đơn",
      required: true,
    },
  ],
};

describe("CapabilityGallery", () => {
  test("renders one card per registered spec with VN labels + slots", () => {
    render(
      <CapabilityGallery
        specs={[recon]}
        busy={false}
        onIntent={rs.fn()}
        onGoverned={rs.fn()}
      />,
    );
    expect(screen.getByText("Đối soát hóa đơn")).toBeTruthy();
    expect(screen.getByText(/Zip hóa đơn/)).toBeTruthy();
  });

  test("starter-intent chips call onIntent", () => {
    const onIntent = rs.fn();
    render(
      <CapabilityGallery
        specs={[recon]}
        busy={false}
        onIntent={onIntent}
        onGoverned={rs.fn()}
      />,
    );
    fireEvent.click(screen.getByText(/Kiểm tra hóa đơn đầu vào kỳ 09\/2025/));
    expect(onIntent).toHaveBeenCalledTimes(1);
  });

  test("unregistered pack renders the disabled coming-soon card", () => {
    render(
      <CapabilityGallery
        specs={[recon]}
        busy={false}
        onIntent={rs.fn()}
        onGoverned={rs.fn()}
      />,
    );
    expect(screen.getByText("Đối chiếu sao kê ngân hàng")).toBeTruthy();
    expect(screen.getByText(/Sắp có/)).toBeTruthy();
  });

  test("registered coming-soon pack drops the placeholder", () => {
    const bank: OutcomeSpecView = {
      outcome_type: "bank_recon",
      description: "x",
      required_checks: [],
      requires_inputs: [],
    };
    render(
      <CapabilityGallery
        specs={[recon, bank]}
        busy={false}
        onIntent={rs.fn()}
        onGoverned={rs.fn()}
      />,
    );
    expect(screen.queryByText(/Sắp có/)).toBeNull();
  });

  test("Chạy kiểm chứng button calls onGoverned with the card's spec", () => {
    const onGoverned = rs.fn();
    render(
      <CapabilityGallery
        specs={[recon]}
        busy={false}
        onIntent={rs.fn()}
        onGoverned={onGoverned}
      />,
    );
    fireEvent.click(screen.getByText("Chạy kiểm chứng"));
    expect(onGoverned).toHaveBeenCalledTimes(1);
    expect(onGoverned.mock.calls[0]![0].outcome_type).toBe("invoice_recon");
  });

  test("fallback specs render when the registry is unreachable", () => {
    render(
      <CapabilityGallery
        specs={FALLBACK_SPECS}
        busy={false}
        onIntent={rs.fn()}
        onGoverned={rs.fn()}
      />,
    );
    expect(screen.getByText("Kiểm tra chứng từ")).toBeTruthy();
  });
});
