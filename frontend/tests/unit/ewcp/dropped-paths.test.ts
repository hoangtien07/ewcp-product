// A3 Task 7 DoD guard: the legacy pane and its proxy hunks are dropped —
// nothing may reference the removed routes, files, or components again.
// The live replacements: /workspace/ewcp-runs (workspace surface),
// /verify/[hash] (public permalink), /api/ewcp/* (extension routes).

import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "@rstest/core";

const FRONTEND_ROOT = path.resolve(__dirname, "../../..");
const REPO_ROOT = path.resolve(FRONTEND_ROOT, "..");

function* walk(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      yield* walk(full);
    } else if (entry.isFile()) {
      yield full;
    }
  }
}

function frontendSources(): string[] {
  return [...walk(path.join(FRONTEND_ROOT, "src"))].filter((f) =>
    /\.(ts|tsx|js|jsx|md|mdx)$/.exec(f),
  );
}

function violations(pattern: RegExp): string[] {
  return frontendSources().filter((f) =>
    pattern.test(readFileSync(f, "utf8")),
  );
}

describe("dropped /ewcp pane paths (A3 Task 7)", () => {
  it("leaves no pane route directory or pane README behind", () => {
    expect(existsSync(path.join(FRONTEND_ROOT, "src/app/ewcp"))).toBe(
      false,
    );
    expect(existsSync(path.join(FRONTEND_ROOT, "src/ewcp/README.md"))).toBe(
      false,
    );
  });

  it("has no references to the legacy pane routes", () => {
    // Matches "/ewcp", '/ewcp', `/ewcp`, /ewcp?…, /ewcp/… — the pane route
    // and its verify subroute. Does not match /workspace/ewcp-runs or
    // /api/ewcp/* (their /ewcp segment is not quote-prefixed).
    const legacyRoute = /["'`]\/ewcp(?:[/?"'`#]|$)/;
    expect(violations(legacyRoute)).toEqual([]);
  });

  it("has no imports of dropped pane components", () => {
    const droppedImport =
      /@\/ewcp\/components\/(?:task-list|task-thread|pack-gallery|general-card)/;
    expect(violations(droppedImport)).toEqual([]);
  });

  it("never leaks the kernel key into the browser client", () => {
    expect(violations(/x-ewcp-api-key/i)).toEqual([]);
  });

  it("keeps the retired proxy hunks out of nginx and next.config", () => {
    for (const conf of [
      "docker/nginx/nginx.conf",
      "docker/nginx/nginx.local.conf",
    ]) {
      const text = readFileSync(path.join(REPO_ROOT, conf), "utf8");
      expect(/location\s+\/api\/ewcp/.test(text)).toBe(false);
    }
    expect(
      readFileSync(path.join(FRONTEND_ROOT, "next.config.js"), "utf8"),
    ).not.toContain("proxyTimeout");
  });
});
