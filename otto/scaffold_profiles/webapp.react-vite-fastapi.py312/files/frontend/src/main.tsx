// Otto-seeded scaffold (profile: webapp.react-vite-fastapi.py312).
// AUTHORITATIVE React-18 root mount — do not rewrite this file. Build the
// product UI in App.tsx and the components/pages it statically imports.
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error('Otto scaffold: #root element missing from index.html');
}

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
