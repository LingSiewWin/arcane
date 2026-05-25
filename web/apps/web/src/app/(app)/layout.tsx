import { TooltipProvider } from "@web/ui/components/tooltip";

import Header from "@/components/header";

export default function AppLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <TooltipProvider>
      <div className="grid h-svh grid-rows-[auto_1fr]">
        <Header />
        <div className="min-h-0 overflow-y-auto">{children}</div>
      </div>
    </TooltipProvider>
  );
}
