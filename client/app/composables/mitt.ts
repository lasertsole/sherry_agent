import mitt from 'mitt';

const emitter = mitt();
export const emit = emitter.emit;// Emit-event method $emit
export const on = emitter.on;// Listen-for-event method $on
export const off = emitter.off;// Cancel-listening method $off

// Expose a test hook in development only: allows tools like Playwright to inject events directly
// into the mitt bus (e.g. ws:notification). In production builds (import.meta.env.DEV === false),
// no global variable is injected.
if (import.meta.env.DEV) {
  (window as unknown as { __emitTest: typeof emit }).__emitTest = emit;
}