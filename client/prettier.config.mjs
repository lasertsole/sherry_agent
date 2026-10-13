/**
 * @see https://prettier.io/docs/en/configuration.html
 * @type {import("prettier").Config}
 */
export default {
  // Max 120 characters per line
  printWidth: 120,
  // Use 2-space indentation
  tabWidth: 2,
  // Use single quotes
  singleQuote: true,
  // Set "semi": true to use semicolons, or false to omit trailing ones
  semi: true,
  // No trailing comma on the last line of multi-line comma-separated syntax
  trailingComma: 'none',
  jsxSingleQuote: true,
  // Omit parentheses for single-parameter arrow functions: x => x
  arrowParens: 'avoid',
  // Whether to add spaces inside object braces: { a:0 }
  bracketSpacing: true,
  // Put the > of multi-line HTML (HTML, JSX, Vue, Angular) elements at the end of the last line
  // instead of on its own line. Defaults to false.
  bracketSameLine: true,
  endOfLine: 'auto',
  // One attribute per line
  singleAttributePerLine: true
};
