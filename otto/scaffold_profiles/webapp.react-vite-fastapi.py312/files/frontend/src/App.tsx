// Otto-seeded scaffold (profile: webapp.react-vite-fastapi.py312).
// PRODUCT ENTRY — extend this file. Add pages and components with ordinary
// top-level static imports (e.g. `import Page from "./pages/Page"`). Do NOT
// defer-load or code-split a module that does not exist yet: the production
// bundler (rollup, `npm run build`) cannot resolve a deferred reference to a
// missing module and the whole build fails. Keep index.html and main.tsx
// as-is — they are the authoritative Vite entry + React root mount.
import { type ReactElement } from "react";

export default function App(): ReactElement {
  return (
    <main>
      <h1>App</h1>
    </main>
  );
}
