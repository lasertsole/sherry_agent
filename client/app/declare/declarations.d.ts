// Ambient module declarations for dependencies Vite resolves but that ship no
// types for the exact entry the code imports.

/**
 * mammoth's prebuilt browser bundle (its main entry needs Node builtins, so
 * the docx viewer imports the UMD build directly).
 */
declare module 'mammoth/mammoth.browser.js' {
  interface MammothResult {
    /** The converted HTML fragment. */
    value: string;
    /** Non-fatal conversion warnings (dropped footnotes, unsupported shapes…). */
    messages: Array<{ type: string; message: string }>;
  }
  interface MammothInput {
    arrayBuffer: ArrayBuffer;
  }
  const mammoth: {
    convertToHtml(input: MammothInput): Promise<MammothResult>;
  };
  export default mammoth;
}
