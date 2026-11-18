/**
 * Paths, CLI parsing and preflight helpers for the packaged-app E2E suite.
 *
 * The suite deliberately runs the *packaged* Tauri app, not `pnpm tauri dev`:
 * the deb layout is extracted into a temporary prefix so the shell resolves
 * `../lib/EMA AI Agent/sherry-backend`. A raw `target/release` binary cannot
 * resolve that resource tree (Tauri looks under `target/lib/...`), so it is
 * rejected with an actionable message.
 */
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const PACKAGED_DIR = path.resolve(HERE, '..');
export const E2E_DIR = path.resolve(HERE, '../..');
export const CLIENT_ROOT = path.resolve(HERE, '../../..');
export const REPO_ROOT = path.resolve(HERE, '../../../..');
export const APP_HOME_DIR = 'com.ema-ai.agent';
export const BACKEND_PORT = 8080;
export const DEFAULT_TIMEOUT_MS = 120_000;
export const DRIVER_PORT = 4444;
export const NATIVE_DRIVER_PORT = 4445;

/** The newest deb built by `pnpm tauri build`, or null. */
export function findDeb() {
  const dir = path.join(CLIENT_ROOT, 'src-tauri/target/release/bundle/deb');
  if (!fs.existsSync(dir)) return null;
  const debs = fs
    .readdirSync(dir)
    .filter(name => name.endsWith('.deb'))
    .map(name => path.join(dir, name))
    .sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  return debs[0] ?? null;
}

/** Extract a deb into `dest` (no root required). */
export function extractDeb(deb, dest) {
  fs.mkdirSync(dest, { recursive: true });
  execFileSync('dpkg-deb', ['-x', deb, dest], { stdio: 'inherit' });
  const app = path.join(dest, 'usr/bin/ema-ai-agent');
  if (!fs.existsSync(app)) {
    throw new Error(`deb did not contain usr/bin/ema-ai-agent: ${deb}`);
  }
  return app;
}

/**
 * Resolve the packaged executable.
 * @param {{app?: string, deb?: string, tempRoot: string}} options
 */
export function resolvePackagedApp(options) {
  if (options.app) {
    if (!fs.existsSync(options.app)) throw new Error(`app not found: ${options.app}`);
    if (options.app.includes(`${path.sep}target${path.sep}release${path.sep}`)) {
      throw new Error(
        'the raw target/release binary cannot resolve the bundled sherry-backend ' +
          '(Tauri looks under /usr/lib/EMA AI Agent). Pass the deb with --deb, or a binary ' +
          'extracted from the deb (usr/bin/ema-ai-agent).'
      );
    }
    return options.app;
  }
  const deb = options.deb || findDeb();
  if (deb) {
    if (!fs.existsSync(deb)) throw new Error(`deb not found: ${deb}`);
    return extractDeb(deb, path.join(options.tempRoot, 'app'));
  }
  const installed = '/usr/bin/ema-ai-agent';
  if (fs.existsSync(installed)) return installed;
  throw new Error(
    'No packaged app found. Build it first with:\n' +
      '  uv run --no-sync python packaging/build_backend.py\n' +
      '  cd client && pnpm tauri build\n' +
      'or pass --deb <path> / --app <installed binary>.'
  );
}

/** Resolve a command from --flag, env var, or PATH. */
export function resolveBinary(explicit, envVar, fallback) {
  const candidates = [explicit, process.env[envVar]].filter(Boolean);
  try {
    candidates.push(execFileSync('which', [fallback], { encoding: 'utf8' }).trim());
  } catch {
    // Not on PATH.
  }
  for (const candidate of candidates) {
    try {
      if (candidate && fs.existsSync(candidate)) return candidate;
    } catch {
      // A broken symlink from the environment shim is not usable.
    }
  }
  return null;
}

/** True when something is listening on the TCP port. */
export function portIsOpen(port, host = '127.0.0.1', timeout = 700) {
  return new Promise(resolve => {
    const socket = net.createConnection({ port, host });
    let done = false;
    const finish = value => {
      if (done) return;
      done = true;
      socket.destroy();
      resolve(value);
    };
    socket.setTimeout(timeout);
    socket.once('connect', () => finish(true));
    socket.once('timeout', () => finish(false));
    socket.once('error', () => finish(false));
  });
}

export async function assertPortFree(port) {
  if (await portIsOpen(port)) {
    throw new Error(
      `port ${port} is already in use. The packaged frontend calls http://localhost:${port}; ` +
        'stop the running backend/app before the E2E run.'
    );
  }
}

export function makeTempRoot(prefix = 'sherry-e2e-packaged-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

export function parseArgs(argv) {
  const options = {
    list: false,
    spike: false,
    feature: null,
    app: null,
    deb: null,
    tauriDriver: null,
    webkitDriver: null,
    display: process.env.DISPLAY || ':98',
    keep: false,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    mockPort: 0
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[++i];
      if (value === undefined) throw new Error(`missing value for ${arg}`);
      return value;
    };
    if (arg === '--') continue;
    else if (arg === '--list') options.list = true;
    else if (arg === '--spike') options.spike = true;
    else if (arg === '--keep') options.keep = true;
    else if (arg === '--feature') options.feature = next();
    else if (arg === '--app') options.app = next();
    else if (arg === '--deb') options.deb = next();
    else if (arg === '--tauri-driver') options.tauriDriver = next();
    else if (arg === '--webkit-driver') options.webkitDriver = next();
    else if (arg === '--display') options.display = next();
    else if (arg === '--timeout') options.timeoutMs = Number(next());
    else if (arg === '--mock-port') options.mockPort = Number(next());
    else if (arg === '--help' || arg === '-h') {
      console.log(
        'usage: node e2e/packaged/run.mjs [--list|--spike] [--feature F05] ' +
          '[--app <path>|--deb <path>] [--tauri-driver <path>] [--webkit-driver <path>] ' +
          '[--display :99] [--timeout ms] [--keep]'
      );
      process.exit(0);
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  return options;
}
