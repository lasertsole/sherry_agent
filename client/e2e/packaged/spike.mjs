#!/usr/bin/env node
/** Thin alias for `run.mjs --spike` (kept as a named entry point). */
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const result = spawnSync(process.execPath, [path.join(here, 'run.mjs'), '--spike', ...process.argv.slice(2)], {
  stdio: 'inherit'
});
process.exit(result.status ?? 2);
