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
  required_context: ["mst_doanh_nghiep", "ky"],
  context_schema: [],
};

const schemaSpec: OutcomeSpecView = {
  outcome_type: "erp_reads",
  description: "d",
  required_checks: [],
  requires_inputs: [],
  required_context: ["capability"],
  context_schema: [
    {
      name: "capability",
      type: "str",
      required: true,
      label_vn: "Capability",
    },
    {
      name: "limit",
      type: "int",
      required: false,
      default: 20,
      label_vn: "Giới hạn dòng",
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
  required_context: [],
  context_schema: [],
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
    const submit = screen.getByText("Chạy kiểm chứng");
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/Còn thiếu đầu vào bắt buộc/)).toBeTruthy();

    pick(fileInputFor("Zip hóa đơn"), ["inv.zip"]);
    pick(fileInputFor("Sổ kế toán"), ["books.csv"]);
    // invoice_recon's required_context (mst_doanh_nghiep, ky) also gates
    fireEvent.change(screen.getByLabelText("mst_doanh_nghiep"), {
      target: { value: "0300000001" },
    });
    fireEvent.change(screen.getByLabelText("ky"), {
      target: { value: "09/2025" },
    });
    expect((submit as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(submit);
    expect(onLaunch).toHaveBeenCalledTimes(1);
    const slotFiles = onLaunch.mock.calls[0]![0] as Record<string, File[]>;
    expect(slotFiles.invoices_zip?.[0]?.name).toBe("inv.zip");
    expect(slotFiles.books?.[0]?.name).toBe("books.csv");
  });

  test("required_context keys render inputs and gate submit (F2)", () => {
    const onLaunch = rs.fn();
    render(
      <GovernedIntake
        spec={spec}
        busy={false}
        onCancel={rs.fn()}
        onLaunch={onLaunch}
      />,
    );
    const submit = screen.getByText("Chạy kiểm chứng");
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    // files alone can't launch — the kernel would 422 missing context
    pick(fileInputFor("Zip hóa đơn"), ["inv.zip"]);
    pick(fileInputFor("Sổ kế toán"), ["books.csv"]);
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("mst_doanh_nghiep"), {
      target: { value: "0300000001" },
    });
    fireEvent.change(screen.getByLabelText("ky"), {
      target: { value: "09/2025" },
    });
    expect((submit as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(submit);
    const ctxValues = onLaunch.mock.calls[0]![1] as Record<string, string>;
    expect(ctxValues.mst_doanh_nghiep).toBe("0300000001");
    expect(ctxValues.ky).toBe("09/2025");
  });

  test("context_schema fields keep label/required/default (F2)", () => {
    const onLaunch = rs.fn();
    render(
      <GovernedIntake
        spec={schemaSpec}
        busy={false}
        onCancel={rs.fn()}
        onLaunch={onLaunch}
      />,
    );
    const limitInput = screen.getByLabelText(/Giới hạn dòng/);
    expect(limitInput.getAttribute("value")).toBe("20");
    expect(screen.getByText("Chạy kiểm chứng").hasAttribute("disabled")).toBe(
      true,
    );

    fireEvent.change(screen.getByLabelText("Capability"), {
      target: { value: "erp.read_doc" },
    });
    fireEvent.click(screen.getByText("Chạy kiểm chứng"));
    const ctxValues = onLaunch.mock.calls[0]![1] as Record<string, string>;
    expect(ctxValues.capability).toBe("erp.read_doc");
    expect(ctxValues.limit).toBe("20");
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
    expect(screen.getByText("Chạy kiểm chứng").hasAttribute("disabled")).toBe(
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
