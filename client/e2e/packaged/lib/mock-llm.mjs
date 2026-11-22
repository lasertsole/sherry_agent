/**
 * Lifecycle wrapper for `mock_llm.py`.
 *
 * The mock is started before the app so `.models` can point at its port. It is
 * killed with the rest of the run; its stdout/stderr are kept in the report.
 */
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { PACKAGED_DIR } from './env.mjs';

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function health(port, timeoutMs = 500) {
  return new Promise(resolve => {
    const request = http.get({ host: '127.0.0.1', port, path: '/health', timeout: timeoutMs }, response => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('timeout', () => {
      request.destroy();
      resolve(false);
    });
    request.on('error', () => resolve(false));
  });
}

/**
 * @param {{port?: number, logDir: string}} options
 */
export async function startMockLlm(options) {
  const port = options.port || (await freePort());
  const python = process.env.SHERRY_E2E_PYTHON || 'python3';
  const script = path.join(PACKAGED_DIR, 'mock_llm.py');
  const logPath = path.join(options.logDir, 'mock-llm.log');
  fs.mkdirSync(options.logDir, { recursive: true });
  const log = fs.openSync(logPath, 'a');
  const child = spawn(python, [script, '--port', String(port)], {
    stdio: ['ignore', log, log],
    env: { ...process.env, PYTHONUNBUFFERED: '1' }
  });
  const started = Date.now();
  let ready = false;
  while (Date.now() - started < 10_000) {
    if (await health(port)) {
      ready = true;
      break;
    }
    if (child.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  fs.closeSync(log);
  if (!ready) {
    child.kill('SIGKILL');
    throw new Error(`mock LLM did not become ready; see ${logPath}`);
  }
  return {
    port,
    logPath,
    stop() {
      if (child.exitCode === null) {
        child.kill('SIGTERM');
        setTimeout(() => {
          if (child.exitCode === null) child.kill('SIGKILL');
        }, 2000).unref();
      }
    }
  };
}
