declare global {
  interface Window {
    readonly __tavernScriptIsolation?: number;
  }
}

// This value is installed before application JS by the native/desktop host.
// A desktop backend alone does not make an ordinary browser window protected.
export const isolatedCardScripts = window.__tavernScriptIsolation === 1;
