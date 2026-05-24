"use client";

import { Terminal } from "lucide-react";

import { Card } from "@web/ui/components/card";

export function EmptyState({ message }: { message?: string }) {
  return (
    <Card className="flex flex-col items-center gap-4 border-dashed p-12 text-center">
      <Terminal className="size-8 text-muted-foreground" />
      <div className="flex flex-col gap-1">
        <h2 className="text-base font-semibold">No complete run artifact</h2>
        <p className="max-w-md text-sm text-muted-foreground">
          {message ??
            "The dashboard renders only real on-chain data. There is no live run to visualize yet."}
        </p>
      </div>
      <code className="rounded-md border border-border/60 bg-muted/40 px-3 py-2 font-mono text-xs">
        bash scripts/go_live.sh
      </code>
      <p className="text-[10px] text-muted-foreground">
        populates <span className="font-mono">scripts/demo_output.jsonl</span> with all six steps
      </p>
    </Card>
  );
}
