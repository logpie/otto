import {spawnSync} from "node:child_process";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {defineConfig, version as viteVersion, type Plugin} from "vite";
import react from "@vitejs/plugin-react";

const root = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(root, "../../..");

/**
 * Run `python scripts/build_stamp.py` after Vite finishes a production
 * build, so every bundle in `otto/web/static/` ships with a matching
 * `build-stamp.json`. The Python helper is the source of truth for the
 * source-hash algorithm (re-used by the runtime check in
 * `otto/web/bundle.py`); duplicating it in TypeScript would let the two
 * implementations drift.
 *
 * The plugin is intentionally a no-op for `vite serve` (dev server) — the
 * stamp is only meaningful for the static bundle that `otto web` serves.
 */
function buildStampPlugin(): Plugin {
  return {
    name: "otto-build-stamp",
    apply: "build",
    closeBundle() {
      const env = {
        ...process.env,
        OTTO_BUILD_VITE_VERSION: viteVersion,
        OTTO_BUILD_NODE_VERSION: process.versions.node,
      };
      const candidates = [process.env.PYTHON, "python3", "python"].filter(
        (cmd): cmd is string => Boolean(cmd),
      );
      let lastError: string | null = null;
      for (const cmd of candidates) {
        const result = spawnSync(cmd, ["scripts/build_stamp.py"], {
          cwd: repoRoot,
          env,
          stdio: "inherit",
        });
        if (result.error && (result.error as NodeJS.ErrnoException).code === "ENOENT") {
          lastError = `${cmd}: not found`;
          continue;
        }
        if (result.status === 0) {
          return;
        }
        lastError = `${cmd} exited with status ${result.status ?? "null"}`;
        break;
      }
      throw new Error(
        `Failed to write otto/web/static/build-stamp.json: ${lastError ?? "no python interpreter"}`,
      );
    },
  };
}

export default defineConfig({
  root,
  base: "/static/",
  plugins: [react(), buildStampPlugin()],
  build: {
    outDir: resolve(root, "../static"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
