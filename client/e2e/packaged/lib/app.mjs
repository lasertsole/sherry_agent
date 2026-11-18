/**
 * App-level user flows shared by the feature stubs.
 *
 * Everything here is expressed as visible actions: dismiss onboarding, create a
 * session through the preset dialog, switch/delete a session, set access mode.
 * No store or HTTP call is used.
 */
import { By, Key, sleep } from './driver.mjs';

export const COMPOSER = '[contenteditable="true"][role="textbox"]';
export const SEND = 'button.send';
export const STOP = 'button.stop';
export const SESSION_SIDEBAR = '[data-test="session-sidebar"]';
export const PRESET_DIALOG = '[data-test="new-session-dialog"]';
export const PRESET_SHERRY = '[data-test="new-session-preset-option-sherry"]';
export const PRESET_CONFIRM = '[data-test="new-session-confirm"]';
export const ONBOARDING_SKIP = '[data-test="onboarding-skip"]';

async function reload(ui) {
  await ui.execute('window.location.reload()');
  await sleep(1500);
  await ui.find('#__nuxt', 60_000);
}

/** Force the English UI and wait until the bundled backend answers. */
export async function prepareApp(ui) {
  await ui.find('#__nuxt', 90_000);
  // The onboarding flag lives in Dexie with a localStorage mirror read first.
  // Writing the mirror before the first shell render keeps the tour out of the
  // way; the same call pins the English locale. Reload so both take effect.
  await ui.execute(`
    window.localStorage.setItem('appFlag:onboarding_completed', 'true');
    document.cookie = 'i18n_redirected=en; path=/; SameSite=Lax';
    window.location.reload();
  `);
  await sleep(1500);
  await ui.find('#__nuxt', 90_000);
  await ui.waitText('New Chat', 90_000);
  // A fresh app boots the bundled backend behind the webview; its first
  // requests may show a transient banner before the health probe succeeds.
  await ui.waitNoText('Backend service unreachable', 90_000);
}

async function ensureEnglish(ui) {
  const text = await ui.bodyText();
  if (text.includes('New Chat')) return;
  await ui.execute("document.cookie='i18n_redirected=en; path=/; SameSite=Lax'");
  await reload(ui);
}

/** Create a session through the mandatory preset dialog and return its id. */
export async function currentSid(ui) {
  const pathname = await ui.pathname();
  const match = String(pathname).match(/\/home\/([^/?#]+)/);
  return match && match[1] !== 'home' ? decodeURIComponent(match[1]) : null;
}

export async function newSession(ui) {
  const previousSid = await currentSid(ui);
  if (!(await ui.exists(PRESET_DIALOG))) {
    await ui.clickText('New Chat');
    await ui.find(PRESET_DIALOG, 15_000);
  }
  await ui.click(PRESET_SHERRY);
  await ui.click(PRESET_CONFIRM);
  const sid = await ui.waitForNewSid(previousSid, 30_000);
  await ui.find(COMPOSER, 30_000);
  await warmAgentSocket();
  return sid;
}

/**
 * The agent socket's first handshake can lose the race against the gateway
 * token fetch; its 5 s reconnect then succeeds. Waiting here keeps the chat
 * stubs from sending into the rejected first socket.
 */
async function warmAgentSocket() {
  await sleep(6500);
}

/** Ensure a session exists and return its id. */
export async function ensureSession(ui) {
  await ensureEnglish(ui);
  const pathname = await ui.pathname();
  const match = String(pathname).match(/\/home\/([^/?#]+)/);
  if (match) {
    await ui.find(COMPOSER, 30_000);
    await warmAgentSocket();
    return decodeURIComponent(match[1]);
  }
  if (await ui.exists(PRESET_DIALOG)) {
    const previousSid = await currentSid(ui);
    await ui.click(PRESET_SHERRY);
    await ui.click(PRESET_CONFIRM);
    const sid = await ui.waitForNewSid(previousSid, 30_000);
    await ui.find(COMPOSER, 30_000);
    await warmAgentSocket();
    return sid;
  }
  return newSession(ui);
}

/** Switch the left sidebar back to the session list when it shows files. */
async function ensureSessionList(ui) {
  if (await ui.exists('[data-test="sidebar-files-panel"]')) {
    await ui.clickAriaPrefix('Sessions');
    await sleep(300);
  }
}

export function sessionRowSelector(sid) {
  return `[data-session-id="${String(sid).replace(/"/g, '\\"')}"]`;
}

export async function sessionRow(ui, sid) {
  return ui.find(sessionRowSelector(sid), 20_000);
}

/** Click one session row by its displayed session id. */
export async function switchToSession(ui, sid) {
  await ensureSessionList(ui);
  const row = await sessionRow(ui, sid);
  await ui.driver.executeScript('arguments[0].click()', row);
  const started = Date.now();
  while (Date.now() - started < 15_000) {
    if ((await ui.pathname()).endsWith(`/home/${sid}`)) return;
    await sleep(200);
  }
  throw new Error(`did not switch to session ${sid}`);
}

/** Delete one session row through the trash + confirm dialog. */
export async function deleteSession(ui, sid) {
  await ensureSessionList(ui);
  const row = await sessionRow(ui, sid);
  const trash = await row.findElement(By.css('button[aria-label="Delete session"]'));
  await ui.driver.executeScript('arguments[0].click()', trash);
  await ui.clickText('Delete');
  const started = Date.now();
  while (Date.now() - started < 15_000) {
    if (!(await ui.exists(sessionRowSelector(sid)))) return;
    await sleep(250);
  }
  throw new Error(`session row survived deletion: ${sid}`);
}

/** Set the session's access mode by its visible label. */
export async function setAccessMode(ui, label) {
  await ui.clickAriaPrefix('Access mode');
  await ui.clickText(label);
  await sleep(300);
}

/**
 * Wait until the current turn has settled and the composer is empty.
 *
 * Chat stubs run back-to-back; without this the next `sendKeys` can land while
 * the previous turn is still streaming, splitting the prompt between the sent
 * bubble and the composer.
 */
export async function waitForIdle(ui, timeout = 90_000) {
  await ui.waitGone(STOP, timeout);
  await ui.find(SEND, 30_000);
  const started = Date.now();
  while (Date.now() - started < 30_000) {
    const text = await (await ui.find(COMPOSER, 10_000)).getText();
    if (!text.trim()) return;
    await sleep(200);
  }
  throw new Error('composer did not clear after the turn');
}

export async function sendMessage(ui, text) {
  await waitForIdle(ui);
  await ui.type(COMPOSER, text);
  await sleep(400);
  await ui.click(SEND);
}

export async function sendWithEnter(ui, text) {
  await waitForIdle(ui);
  await ui.type(COMPOSER, text);
  await sleep(400);
  await ui.pressEnter(COMPOSER);
}

/** Text of the newest assistant bubble. */
export async function lastAiText(ui) {
  const element = await ui.last('[data-message-role="ai"]');
  return element.getText();
}

/** Open the toolbar settings menu (idempotent). */
export async function openMenu(ui) {
  if (!(await ui.exists('.p-menu'))) {
    await ui.clickAriaPrefix('Menu');
    await sleep(300);
  }
}

/** Close a PrimeVue popup menu with Escape. */
export async function closeMenu(ui) {
  await ui.driver.actions().sendKeys(Key.ESCAPE).perform();
  await sleep(200);
}
