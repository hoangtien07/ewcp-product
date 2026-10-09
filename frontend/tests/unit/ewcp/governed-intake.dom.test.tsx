import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { OutcomeSpecView } from "@/ewcp/api";
import { GovernedIntake } from "@/ewcp/components/governed-intake";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const spec: OutcomeSpecView = {
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
    {
      name: "books",
      accept: ".csv,.xlsx",
      label_vn: "Sổ kế toán",
      required: true,
    },
  ],
};

const twoZips: OutcomeSpecView = {
  outcome_type: "dossier_check",
  description: "d",
  required_checks: [],
  requires_inputs: [
    { name: "zip_a", accept: ".zip", label_vn: "Zip A", required: true },
    { name: "zip_b", accept: ".zip", label_vn: "Zip B", required: true },
  ],
};

function fileInputFor(label: string): HTMLInputElement {
  const labelEl = screen.getByText(label);
  const input = labelEl.parentElement?.querySelector("input[type=file]");
  if (!input) throw new Error(`no file input under ${label}`);
  return input as HTMLInputElement;
}

function pick(input: HTMLInputElement, names: string[]) {
  const files = names.map(
    (n) => new File([new Uint8Array([1, 2])], n, { type: "text/plain" }),
  );
  fireEvent.change(input, { target: { files } });
}

describe("GovernedIntake", () => {
  test("submit stays disabled until all required slots have files", () => {
    const onLaunch = rs.fn();
    render(
      <GovernedIntake
        spec={spec}
        busy={false}
        onCancel={rs.fn()}
        onLaunch={onLaunch}
      />,
    );
    const submit = screen.getByText("Chạy governed");
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Còn thiếu đầu vào bắt buộc/)).toBeTruthy();

    pick(fileInputFor("Zip hóa đơn"), ["inv.zip"]);
    pick(fileInputFor("Sổ kế toán"), ["books.csv"]);
    expect((submit as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(submit);
    expect(onLaunch).toHaveBeenCalledTimes(1);
    const slotFiles = onLaunch.mock.calls[0]![0] as Record<string, File[]>;
    expect(slotFiles.invoices_zip?.[0]?.name).toBe("inv.zip");
    expect(slotFiles.books?.[0]?.name).toBe("books.csv");
  });

  test("two filled zip slots trigger the kernel one-zip guard", () => {
    const onLaunch = rs.fn();
    render(
      <GovernedIntake
        spec={twoZips}
        busy={false}
        onCancel={rs.fn()}
        onLaunch={onLaunch}
      />,
    );
    pick(fileInputFor("Zip A"), ["a.zip"]);
    pick(fileInputFor("Zip B"), ["b.zip"]);
    expect(screen.getByText(/một trường zip/)).toBeTruthy();
    expect(screen.getByText("Chạy governed").hasAttribute("disabled")).toBe(
      true,
    );
    expect(onLaunch).not.toHaveBeenCalled();
  });

  test("cancel calls onCancel", () => {
    const onCancel = rs.fn();
    render(
      <GovernedIntake
        spec={spec}
        busy={false}
        onCancel={onCancel}
        onLaunch={rs.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Huỷ"));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
