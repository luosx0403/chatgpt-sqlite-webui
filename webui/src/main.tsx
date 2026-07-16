import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import { applySettings, loadSettings } from "./settings";

applySettings(loadSettings());

const root = createRoot(document.getElementById("root")!);
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

// Local host integrations and lifecycle tests can request a deterministic
// React teardown without depending on browser navigation timing.
window.addEventListener("chatgpt-archive:teardown", () => root.unmount(), { once: true });
