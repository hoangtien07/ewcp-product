import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

import type { OutcomeSpecView } from "@/ewcp/api";
import { PackGallery } from "@/ewcp/components/pack-gallery";
import { bindPresets, FALLBACK_SPECS } from "@/ewcp/registry";

const recon: OutcomeSpecView = {
  outcome_type: "invoice_recon",
  description: "Đối soát hóa đơn đầu vào 1 kỳ.",
  required_checks: ["totals_match"],
  requires_inputs: [
    {
      name: "invoices_zip",
      accept: ".zip",
      label_vn: "Zip hóa đơn",
      required: true,
    },
    {
      name: "books",
      accept: ".csv",
      label_vn: "Sổ kế toán",
      required: false,
    },
  ],
};

const dossier: OutcomeSpecView = {
  outcome_type: "dossier_check",
  description: "Kiểm tra hồ sơ chứng từ.",
  required_checks: [],
  requires_inputs: [
    {
      name: "dossier_zip",
      accept: ".zip",
      label_vn: "Zip chứng từ",
      required: true,
    },
  ],
};

afterEach(() => {
  cleanup();
});

function renderGallery(specs: OutcomeSpecView[]) {
  const onIntent = rs.fn();
  const onPreset = rs.fn();
  const view = render(
    <PackGallery
      specs={specs}
      presets={bindPresets(specs)}
      busy={false}
      onIntent={onIntent}
      onPreset={onPreset}
    />,
  );
  return { onIntent, onPreset, ...view };
}

describe("PackGallery — catalog cards from the registry", () => {
  test("one card per spec: VN label, description, slots, Governed badge", () => {
    renderGallery([recon, dossier]);
    expect(screen.getByText("Đối soát hóa đơn")).toBeTruthy();
    expect(screen.getByText("Kiểm tra chứng từ")).toBeTruthy();
    expect(screen.getByText("Đối soát hóa đơn đầu vào 1 kỳ.")).toBeTruthy();
    // declared input slots with the optional marker
    expect(screen.getByText("Zip hóa đơn")).toBeTruthy();
    expect(screen.getByText("Sổ kế toán (tuỳ chọn)")).toBeTruthy();
    // one badge per registered card
    expect(
      screen.getAllByText("Governed — niêm phong kiểm chứng"),
    ).toHaveLength(2);
  });

  test("starter-intent chips fill the intent box", () => {
    const { onIntent } = renderGallery([recon]);
    const chips = screen.getAllByTitle("Điền vào ô yêu cầu");
    expect(chips.length).toBeGreaterThanOrEqual(1);
    expect(chips.length).toBeLessThanOrEqual(2);
    fireEvent.click(chips[0]!);
    expect(onIntent).toHaveBeenCalledWith("Đối soát hóa đơn kỳ 09/2025");
  });

  test("sample-data button only where a fixture mapping exists", () => {
    const { onPreset } = renderGallery([recon, dossier]);
    // recon has 2 presets, dossier 1 — all bound through onPreset
    for (const label of ["đối soát", "đối soát (file lỗi)"]) {
      fireEvent.click(screen.getByText(label));
    }
    expect(onPreset).toHaveBeenCalledTimes(2);
    expect(onPreset.mock.calls[0]![0].outcomeType).toBe("invoice_recon");

    cleanup();
    const unmapped: OutcomeSpecView = {
      outcome_type: "no_demo_pack",
      description: "x",
      required_checks: [],
      requires_inputs: [],
    };
    renderGallery([unmapped]);
    expect(screen.queryByText("Dữ liệu mẫu:")).toBeNull();
  });

  test("'Sắp có' card: exactly one, disabled, no controls", () => {
    const { container } = renderGallery([recon, dossier]);
    const cards = screen.getAllByText("Sắp có — chưa mở");
    expect(cards).toHaveLength(1);
    const card = container.querySelector("[aria-disabled='true']")!;
    expect(card).toBeTruthy();
    expect(card.textContent).toContain("Đối chiếu sao kê ngân hàng");
    // non-clickable: no buttons or links inside the placeholder card
    expect(card.querySelectorAll("button, a")).toHaveLength(0);
  });

  test("a registered bank_recon replaces the placeholder card", () => {
    const live: OutcomeSpecView = {
      outcome_type: "bank_recon",
      description: "Đối chiếu sao kê.",
      required_checks: [],
      requires_inputs: [],
    };
    renderGallery([recon, live]);
    // still exactly one card for the pack — now a real governed card
    expect(screen.queryByText("Sắp có — chưa mở")).toBeNull();
    expect(
      screen.getAllByText("Đối chiếu sao kê ngân hàng"),
    ).toHaveLength(1);
    expect(
      screen.getAllByText("Governed — niêm phong kiểm chứng"),
    ).toHaveLength(2);
  });

  test("fallback specs render the same gallery (pre-registry kernel)", () => {
    renderGallery(FALLBACK_SPECS);
    expect(screen.getByText("Kiểm tra chứng từ")).toBeTruthy();
    expect(screen.getByText("Đối soát hóa đơn")).toBeTruthy();
    expect(screen.getAllByText("Sắp có — chưa mở")).toHaveLength(1);
  });
});
