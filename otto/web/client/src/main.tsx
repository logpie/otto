import React from "react";
import {createRoot} from "react-dom/client";
import {App} from "./App";
import "./styles.css";

const root = document.querySelector("#root");

if (!root) {
  throw new Error("Mission Control root element is missing");
}

createRoot(root).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
