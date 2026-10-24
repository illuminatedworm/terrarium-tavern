declare global {
  interface Window {
    readonly __tavernScriptIsolation?: number;
  }
}

// The Android host installs this value before application JS and pauses card
// scripts when its WebView cannot. Desktop browsers keep running them as before
// the Android port; `--desktop` windows install the same guard on top.
export const cardScriptsAllowed = !window.TavernAndroid || window.__tavernScriptIsolation === 1;
