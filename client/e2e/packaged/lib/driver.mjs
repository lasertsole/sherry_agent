/**
 * tauri-driver / WebDriver lifecycle for the packaged app.
 *
 * Tauri's release binary honors `TAURI_WEBVIEW_AUTOMATION` (set by
 * tauri-driver), so the installed/packaged WebKitGTK webview is driven through
 * W3C WebDriver. Playwright is intentionally not used: WebKitGTK exposes no
 * CDP endpoint.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync, spawn } from 'node:child_process';
import { CLIENT_ROOT, DRIVER_PORT, NATIVE_DRIVER_PORT, portIsOpen } from './env.mjs';

const require = createRequire(path.join(CLIENT_ROOT, 'package.json'));
const webdriver = require('selenium-webdriver');
export const { By, Capabilities, Key, until } = webdriver;

export function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** Start Xvfb only when the configured display socket is absent. */
export async function ensureDisplay(display) {
  const number = String(display || ':98').replace(/^:/, '');
  const socket = `/tmp/.X11-unix/X${number}`;
  if (fs.existsSync(socket)) return { display, child: null };
  const child = spawn('Xvfb', [`:${number}`, '-screen', '0', '1280x900x24', '-nolisten', 'tcp'], {
    stdio: 'ignore',
    detached: true
  });
  const started = Date.now();
  while (Date.now() - started < 5000) {
    if (fs.existsSync(socket)) return { display: `:${number}`, child };
    await sleep(100);
  }
  child.kill('SIGKILL');
  throw new Error(`Xvfb did not create ${socket}`);
}

const WEBKIT_ENV = {
  WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS: '1',
  WEBKIT_DISABLE_DMABUF_RENDERER: '1',
  WEBKIT_DISABLE_COMPOSITING_MODE: '1',
  LIBGL_ALWAYS_SOFTWARE: '1',
  GALLIUM_DRIVER: 'llvmpipe',
  MESA_LOADER_DRIVER_OVERRIDE: 'llvmpipe',
  GDK_BACKEND: 'x11'
};

/**
 * Launch tauri-driver with the app environment.
 * @param {{binary: string, nativeDriver: string, display: string, tempRoot: string, logDir: string}} options
 */
export async function startTauriDriver(options) {
  const logPath = path.join(options.logDir, 'tauri-driver.log');
  const log = fs.openSync(logPath, 'a');
  const env = {
    ...process.env,
    ...WEBKIT_ENV,
    DISPLAY: options.display,
    XDG_DATA_HOME: path.join(options.tempRoot, 'XDG_DATA_HOME'),
    SHERRY_PROJECT_DIR: options.projectDir
  };
  const child = spawn(
    options.binary,
    [
      '--port',
      String(DRIVER_PORT),
      '--native-port',
      String(NATIVE_DRIVER_PORT),
      '--native-driver',
      options.nativeDriver
    ],
    { env, stdio: ['ignore', log, log], detached: true }
  );
  fs.closeSync(log);
  const started = Date.now();
  while (Date.now() - started < 10_000) {
    if (await portIsOpen(DRIVER_PORT)) return { child, logPath, env };
    if (child.exitCode !== null) {
      throw new Error(`tauri-driver exited with ${child.exitCode}; see ${logPath}`);
    }
    await sleep(200);
  }
  child.kill('SIGKILL');
  throw new Error(`tauri-driver did not listen on ${DRIVER_PORT}; see ${logPath}`);
}

/** Create one WebDriver session for the packaged app. */
export async function createSession(appPath) {
  const caps = new Capabilities();
  caps.setBrowserName('wry');
  caps.set('tauri:options', { application: appPath });
  const driver = await new webdriver.Builder()
    .usingServer(`http://127.0.0.1:${DRIVER_PORT}`)
    .withCapabilities(caps)
    .build();
  await driver.manage().setTimeouts({ implicit: 0, pageLoad: 120_000, script: 60_000 });
  try {
    await driver.manage().window().setRect({ width: 1600, height: 1000 });
  } catch {
    // Some WebKitGTK builds ignore window geometry; the default is usable.
  }
  return driver;
}

export async function quitApp(driver) {
  try {
    await driver.executeScript("window.__TAURI_INTERNALS__ && window.__TAURI_INTERNALS__.invoke('quit_app')");
  } catch {
    // Session may already be closing.
  }
  await sleep(1200);
  try {
    await driver.quit();
  } catch {
    // Ignore a session the app closed first.
  }
}

/** Kill the tauri-driver process group (native driver + app). */
export function stopProcessGroup(child) {
  if (!child || child.exitCode !== null) return;
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch {
    try {
      child.kill('SIGTERM');
    } catch {
      // Already gone.
    }
  }
  setTimeout(() => {
    if (child.exitCode !== null) return;
    try {
      process.kill(-child.pid, 'SIGKILL');
    } catch {
      // Already gone.
    }
  }, 2000).unref();
}

/** Kill the bundled backend/app left behind by a killed webview session. */
export function killOrphans(appHome, appPath) {
  let rows;
  try {
    rows = execFileSync('ps', ['-eo', 'pid=,args='], { encoding: 'utf8' });
  } catch {
    return;
  }
  const homeMarker = `--sherry-home ${appHome}`;
  for (const line of rows.split('\n')) {
    const match = line.trim().match(/^(\d+)\s+(.*)$/);
    if (!match) continue;
    const pid = Number(match[1]);
    const command = match[2];
    if (pid === process.pid) continue;
    if (command.includes(homeMarker) || command.includes(appPath)) {
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        // Already gone.
      }
    }
  }
}
