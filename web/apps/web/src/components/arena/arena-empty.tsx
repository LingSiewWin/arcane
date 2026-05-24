"use client";

import { Terminal } from "lucide-react";
import type { ReactNode } from "react";

import { Card } from "@web/ui/components/card";

/** Honest empty / not-configured panel. Never renders mock agents. */
export function ArenaEmpty({
  title,
  children,
  cmd,
  hint,
}: {
  title: string;
  children: ReactNode;
  cmd?: string;
  hint?: ReactNode;
}) {
  return (
    <Card className="flex flex-col items-center gap-4 border-dashed p-12 text-center">
      <Terminal className="size-8 text-muted-foreground" />
      <div className="flex max-w-md flex-col gap-1">
        <h2 className="text-base font-semibold">{title}</h2>
        <p className="text-sm text-muted-foreground">{children}</p>
      </div>
      {cmd ? (
        <code className="rounded-md border border-border/60 bg-muted/40 px-3 py-2 font-mono text-xs">
          {cmd}
        </code>
      ) : null}
      {hint ? <p className="text-[10px] text-muted-foreground">{hint}</p> : null}
    </Card>
  );
}
