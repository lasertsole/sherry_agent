/**
 * Isolated app home and model config for one packaged-app run.
 *
 * `XDG_DATA_HOME` is redirected into the run's temp root; the Tauri shell then
 * passes `<root>/XDG_DATA_HOME/com.ema-ai.agent` as `--sherry-home`. Writing
 * `.models` and `sherry.jsonc` before the app starts makes the bundled backend
 * call the local mock model and resolve tool paths against a fixture workspace.
 */
import fs from 'node:fs';
import path from 'node:path';
import { APP_HOME_DIR } from './env.mjs';

/**
 * @param {{tempRoot: string, mockPort: number}} options
 */
export function seedApp(options) {
  const xdgDataHome = path.join(options.tempRoot, 'XDG_DATA_HOME');
  const appHome = path.join(xdgDataHome, APP_HOME_DIR);
  const workspace = path.join(options.tempRoot, 'workspace');
  fs.mkdirSync(appHome, { recursive: true });
  fs.mkdirSync(workspace, { recursive: true });
  const fixture = path.join(workspace, 'e2e-fixture.txt');
  fs.writeFileSync(fixture, 'SHERRY_E2E_FIXTURE\n', 'utf8');

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
