import mitt from 'mitt';

const emitter = mitt();
export const emit = emitter.emit;
export const on = emitter.on;
export const off = emitter.off;

declare global {
  interface Window {
    /** Playwright test hook: inject mitt events directly (development builds only). */
    __emitTest?: typeof emit;
  }
}

// Expose a test hook in development only: allows tools like Playwright to inject events directly
// into the mitt bus (e.g. ws:notification). In production builds (import.meta.env.DEV === false),
// no global variable is injected.
if (import.meta.env.DEV) {
  window.__emitTest = emit;
}
