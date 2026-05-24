import { fileURLToPath } from "node:url";

import "@web/env/web";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  typedRoutes: true,
  reactCompiler: true,
  turbopack: {
    // Pin the workspace root so Next doesn't pick up unrelated lockfiles above.
    root: fileURLToPath(new URL("../..", import.meta.url)),
  },
};

export default nextConfig;
