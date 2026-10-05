"use client";

// ShareVerifyLink — distribution wedge: from a sealed manifest, mint a
// public permalink (GET /verify/{hash} is unauthenticated by design — the
// hash is the capability) and copy it for a third party (sếp, đối tác) to
// inspect the sealed evidence without holding an API key.

import { useEffect, useRef, useState } from "react";

import { verifyShareUrl } from "@/ewcp/api";

type CopyState = "idle" | "copied" | "failed";

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // insecure context / permission denied / no clipboard API → legacy path
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

export function ShareVerifyLink({ manifestHash }: { manifestHash: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const url = verifyShareUrl(manifestHash);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  async function copy() {
    const ok = await copyText(url);
    setState(ok ? "copied" : "failed");
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), 2500);
  }

  return (
    <div className="space-y-1">
      <button
        onClick={copy}
        className={`rounded-md px-3 py-1.5 text-xs font-medium ${
          state === "copied"
            ? "bg-emerald-600 text-white"
            : "bg-zinc-200 hover:bg-zinc-300 dark:bg-zinc-700 dark:hover:bg-zinc-600"
        }`}
      >
        {state === "copied" ? "Đã copy link" : "Chia sẻ link xác minh"}
      </button>
      <p className="text-xs text-zinc-500">
        Người nhận mở link này tra cứu bằng chứng niêm phong — không cần API
        key.
      </p>
      {state === "failed" && (
        <p className="break-all font-mono text-xs text-zinc-500">
          Copy thủ công: {url}
        </p>
      )}
    </div>
  );
}
