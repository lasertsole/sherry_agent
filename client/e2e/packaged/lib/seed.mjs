/**
 * Isolated app home and model config for one packaged-app run.
 *
 * `XDG_DATA_HOME` is redirected into the run's temp root; the Tauri shell then
 * passes `<root>/XDG_DATA_HOME/com.ema-ai.agent` as `--sherry-home`. Writing
 * `.models` and `sherry.jsonc` before the app starts makes the bundled backend
 * call the local mock model and resolve tool paths against a fixture workspace.
 */
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { APP_HOME_DIR } from './env.mjs';

/**
 * @param {{tempRoot: string, mockPort: number}} options
 */
export function seedApp(options) {
  const xdgDataHome = path.join(options.tempRoot, 'XDG_DATA_HOME');
  const appHome = path.join(xdgDataHome, APP_HOME_DIR);
  // The project directory sits at a FIXED host path so the UI's folder picker
  // (which starts at Home) can reach it; the app-data tree stays isolated.
  const workspace = path.join(os.homedir(), 'sherry-e2e-workspace');
  fs.rmSync(workspace, { recursive: true, force: true });
  fs.mkdirSync(appHome, { recursive: true });
  fs.mkdirSync(workspace, { recursive: true });
  const fixture = path.join(workspace, 'e2e-fixture.txt');
  fs.writeFileSync(fixture, 'SHERRY_E2E_FIXTURE\n', 'utf8');
  fs.writeFileSync(path.join(workspace, 'notes.md'), '# E2E Notes\n\nA markdown fixture with **bold** text.\n', 'utf8');
  fs.writeFileSync(path.join(workspace, 'script.py'), 'def greet(name):\n    return f"hello {name}"\n', 'utf8');
  // A real git repo so the Git Graph panel has a branch/commits to render.
  try {
    const run = (args, cwd) => execFileSync('git', args, { cwd });
    run(['init', '-q', '-b', 'main'], workspace);
    run(['-c', 'user.email=e2e@local', '-c', 'user.name=e2e', 'add', '-A'], workspace);
    run(['-c', 'user.email=e2e@local', '-c', 'user.name=e2e', 'commit', '-q', '-m', 'e2e seed'], workspace);
    fs.appendFileSync(path.join(workspace, 'notes.md'), 'dirty line\n', 'utf8');
  } catch {
    // No git on the host: the graph panel exercises its not-a-repo state instead.
  }

  const base = `http://127.0.0.1:${options.mockPort}/v1`;
  const models = {
    MAIN_LLM_PROVIDER: 'openai',
    MAIN_LLM_NAME: 'mock-model',
    MAIN_LLM_API_BASE: base,
    MAIN_LLM_API_KEY: 'e2e-key',
    MAIN_LLM_MAX_TOKEN: 200000,
    MAIN_LLM_MAX_OUTPUT_TOKEN: '',
    MAIN_LLM_ENABLE_THINKING: false,
    AUXILIARY_LLM_MODEL_LOCAL: false,
    AUXILIARY_LLM_PROVIDER: 'openai',
    AUXILIARY_LLM_API_NAME: 'mock-model',
    AUXILIARY_LLM_API_BASE: base,
    AUXILIARY_LLM_API_KEY: 'e2e-key',
    AUXILIARY_LLM_MAX_TOKEN: 200000,
    AUXILIARY_LLM_MAX_OUTPUT_TOKEN: ''
  };
  fs.writeFileSync(path.join(appHome, '.models'), JSON.stringify(models, null, 2) + '\n', 'utf8');
  fs.writeFileSync(
    path.join(appHome, 'sherry.jsonc'),
    JSON.stringify({ project_dir: workspace }, null, 2) + '\n',
    'utf8'
  );
  return { xdgDataHome, appHome, workspace, fixture };
}
