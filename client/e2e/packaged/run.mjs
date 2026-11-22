#!/usr/bin/env node
/**
 * Packaged-app E2E runner.
 *
 * Flow: resolve the packaged deb → start the mock model → seed an isolated
 * `XDG_DATA_HOME` → start tauri-driver → open one WebDriver session → run the
 * selected feature stubs serially → write a report and clean up.
 *
 * Usage:
 *   node e2e/packaged/run.mjs --list
 *   node e2e/packaged/run.mjs --spike
 *   node e2e/packaged/run.mjs [--feature F05] [--keep]
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  BACKEND_PORT,
  CLIENT_ROOT,
  assertPortFree,
  makeTempRoot,
  parseArgs,
  resolveBinary,
  resolvePackagedApp
} from './lib/env.mjs';
import { seedApp } from './lib/seed.mjs';
import { startMockLlm } from './lib/mock-llm.mjs';
import {
  createSession,
  ensureDisplay,
  killOrphans,
  quitApp,
  sleep,
  startTauriDriver,
  stopProcessGroup
} from './lib/driver.mjs';
import { Ui } from './lib/ui.mjs';
import { ensureSession, prepareApp } from './lib/app.mjs';
import { Report } from './lib/report.mjs';
import { FEATURES } from './features/index.mjs';

function timestamp() {
  return new Date().toISOString().replace(/[:.]/g, '-');
}

function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.list) {
    for (const feature of FEATURES) {
      console.log(`${feature.id}\t${feature.status}\t${feature.name}`);
    }
    return 0;
  }
  const wantedIds = options.feature
    ? options.feature
        .split(',')
        .map(id => id.trim().toLowerCase())
        .filter(Boolean)
    : null;
  const selected = wantedIds ? FEATURES.filter(feature => wantedIds.includes(feature.id.toLowerCase())) : FEATURES;
  if (!selected.length) {
    throw new Error(`unknown feature: ${options.feature}`);
  }

  const stamp = timestamp();
  const reportDir = path.join(CLIENT_ROOT, 'e2e-report', `packaged-${stamp}`);
  const logDir = path.join(reportDir, 'logs');
  const report = new Report(reportDir);
  const tempRoot = makeTempRoot();
  const cleanup = {
    driver: null,
    tauriDriver: null,
    mock: null,
    xvfb: null,
    appHome: null,
    appPath: null
  };
  let failed = 0;
  let skipped = 0;

  try {
    const appPath = resolvePackagedApp({ ...options, tempRoot });
    cleanup.appPath = appPath;
    const tauriDriver = resolveBinary(options.tauriDriver, 'TAURI_DRIVER_BIN', 'tauri-driver');
    if (!tauriDriver) {
      throw new Error(
        'tauri-driver is missing. Install it with `cargo install tauri-driver --locked` ' +
          'or pass --tauri-driver <path>.'
      );
    }
    const webkitDriver = resolveBinary(options.webkitDriver, 'WEBKIT_WEBDRIVER_BIN', 'WebKitWebDriver');
    if (!webkitDriver) {
      throw new Error(
        'WebKitWebDriver is missing. Install webkit2gtk-driver (root), or download the deb and ' +
          'extract it with `dpkg-deb -x`, then pass --webkit-driver <path>.'
      );
    }
    await assertPortFree(BACKEND_PORT);
    const display = await ensureDisplay(options.display);
    cleanup.xvfb = display.child;

    cleanup.mock = await startMockLlm({ port: options.mockPort, logDir });
    const seeded = seedApp({ tempRoot, mockPort: cleanup.mock.port });
    cleanup.appHome = seeded.appHome;

    cleanup.tauriDriver = await startTauriDriver({
      binary: tauriDriver,
      nativeDriver: webkitDriver,
      display: display.display,
      tempRoot,
      logDir,
      projectDir: seeded.workspace
    });
    cleanup.driver = await createSession(appPath);
    const ui = new Ui(cleanup.driver, reportDir);
    await prepareApp(ui);

    if (options.spike) {
      const sid = await ensureSession(ui);
      const body = await ui.bodyText();
      if (!body.trim()) throw new Error('packaged app rendered an empty body');
      const screenshot = await ui.screenshot('spike');
      console.log(`SPIKE PASS session=${sid} screenshot=${screenshot}`);
      report.record({ id: 'SPIKE', name: 'Packaged app WebDriver startup' }, { status: 'PASS', durationMs: 0 });
      const summary = report.write();
      console.log(`report: ${reportDir}`);
      return summary.failed ? 1 : 0;
    }

    let sid = await ensureSession(ui);
    console.log(`session: ${sid}`);
    const ctx = { ui, sid, state: {}, reportDir, appHome: seeded.appHome };
    for (const feature of selected) {
      // A frozen WebView makes every later command hang forever; probe with a
      // hard deadline and rebuild the app session when it does not answer.
      const alive = await Promise.race([
        ui
          .execute('return 1')
          .then(() => true)
          .catch(() => false),
        new Promise(resolve => setTimeout(() => resolve(false), 15_000))
      ]);
      if (!alive) {
        console.log(`[${feature.id}] app frozen before the feature; restarting the session`);
        try {
          await quitApp(cleanup.driver);
        } catch {
          /* already gone */
        }
        killOrphans(seeded.appHome, appPath);
        cleanup.driver = await createSession(appPath);
        ctx.ui = new Ui(cleanup.driver, reportDir);
        sid = await ensureSession(ctx.ui);
        console.log(`session restarted: ${sid}`);
        ctx.sid = sid;
      }
      if (feature.status !== 'implemented') {
        report.record(feature, {
          status: 'SKIP',
          reason: feature.reason || 'stub not implemented yet'
        });
        skipped += 1;
        continue;
      }
      const started = Date.now();
      try {
        await withTimeout(feature.run(ctx), options.timeoutMs, feature.id);
        report.record(feature, { status: 'PASS', durationMs: Date.now() - started });
      } catch (error) {
        failed += 1;
        let screenshot = null;
        try {
          screenshot = await ui.screenshot(feature.id.toLowerCase());
        } catch {
          // The session may be gone; the report still records the failure.
        }
        report.record(feature, {
          status: 'FAIL',
          durationMs: Date.now() - started,
          error,
          screenshot
        });
      }
    }
    report.write();
    console.log(`report: ${reportDir}`);
    console.log(`temp app home: ${seeded.appHome}`);
    return failed ? 1 : skipped ? 2 : 0;
  } catch (error) {
    console.error(`FATAL: ${error.stack || error}`);
    if (cleanup.driver) {
      try {
        await new Ui(cleanup.driver, reportDir).screenshot('setup-failure');
      } catch {
        // Best effort.
      }
    }
    report.record(
      { id: 'SETUP', name: 'Packaged app setup' },
      { status: 'BLOCKED', error, reason: 'environment/setup failure' }
    );
    report.write();
    console.error(`report: ${reportDir}`);
    return 2;
  } finally {
    if (cleanup.driver) await quitApp(cleanup.driver);
    if (cleanup.tauriDriver) stopProcessGroup(cleanup.tauriDriver.child);
    if (cleanup.appHome) killOrphans(cleanup.appHome, cleanup.appPath || '');
    if (cleanup.mock) cleanup.mock.stop();
    if (cleanup.xvfb) {
      try {
        cleanup.xvfb.kill('SIGTERM');
      } catch {
        // Already gone.
      }
    }
    await sleep(500);
    if (!options.keep && failed === 0 && skipped === 0) {
      fs.rmSync(tempRoot, { recursive: true, force: true });
    } else {
      console.log(`kept temp root: ${tempRoot}`);
    }
  }
}

main()
  .then(code => {
    process.exitCode = code;
  })
  .catch(error => {
    console.error(error.stack || error);
    process.exitCode = 2;
  });
