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

/**
 * Switch the left sidebar back to the session list. The nav is now a registry
 * (UI upgrade S1): every item has a `nav-{id}` hook, so clicking the chat icon
 * activates the session list from whichever panel is showing. Idempotent.
 */
async function ensureSessionList(ui) {
  const navChat = '[data-test="nav-chat"]';
  if (await ui.exists(navChat)) {
    await ui.click(navChat);
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
  await sleep(400);
  await ui.clickTextDeep(label).catch(async () => {
    await ui.clickAriaPrefix('Access mode');
    await sleep(600);
    await ui.clickTextDeep(label);
  });
  // The ToolbarPopover closes on OUTSIDE pointerdown only; a JS click does
  // not fire one, so dismiss with a real pointer press on a safe area.
  await ui.nativeClick('[data-test="sidebar-title-row"]');
  await sleep(300);
}

/** Empty the composer (leftover trigger characters from earlier stubs). */
export async function clearComposer(ui) {
  await ui.execute(`
    const el = document.querySelector('[contenteditable="true"][role="textbox"]');
    if (!el) return;
    el.focus();
    el.textContent = '';
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContent' }));
  `);
  await sleep(200);
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
  await ensureNoEditMode(ui);
  await clearComposer(ui);
  await ui.type(COMPOSER, text);
  await sleep(400);
  await ui.click(SEND);
}

export async function sendWithEnter(ui, text) {
  await waitForIdle(ui);
  await ensureNoEditMode(ui);
  await clearComposer(ui);
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

/** Dispatch a (synthetic) keyboard chord the app's listeners will see. */
export async function pressChord(ui, key, { ctrl = false, shift = false, code } = {}) {
  await ui.execute(
    `const ev = new KeyboardEvent('keydown', {
       key: arguments[0], code: arguments[3], ctrlKey: arguments[1], shiftKey: arguments[2],
       bubbles: true, cancelable: true,
     });
     document.dispatchEvent(ev);
     window.dispatchEvent(ev);`,
    key,
    ctrl,
    shift,
    code || ''
  );
  await sleep(400);
}

/** Exit edit mode when a previous feature left the banner up. */
async function ensureNoEditMode(ui) {
  if (await ui.exists('[data-test="chat-edit-banner"]')) {
    const cancelled = await ui.clickIfPresent('[data-test="chat-edit-cancel"]');
    if (!cancelled) await closeDialog(ui);
    await ui.waitGone('[data-test="chat-edit-banner"]', 5_000).catch(() => {});
  }
}

/** Open the settings nine-grid and click one tile (data-test suffix). */
export async function openMenuTile(ui, tile) {
  await openMenu(ui);
  await ui.click(`[data-test="settings-menu-${tile}"]`);
  await sleep(500);
}

/** Open the toolbox dialog and click one tool tile. */
export async function openToolbox(ui, tool) {
  await ui.click('[data-test="toolbox-button"]');
  await ui.find('[data-test="toolbox-dialog"]', 10_000);
  await ui.click(`[data-test="toolbox-${tool}"]`);
  await sleep(500);
}

/** Close a PrimeVue dialog/menu with Escape. */
export async function closeDialog(ui) {
  await ui.driver.actions().sendKeys(Key.ESCAPE).perform();
  await sleep(300);
}

/** Close the newest right-sidebar tab (tab-close-<kind>). */
export async function closeRightTab(ui) {
  const closers = await ui.findAll('[data-test^="tab-close-"]');
  if (!closers.length) return;
  await ui.driver.executeScript('arguments[0].click()', closers[closers.length - 1]);
  await sleep(400);
}

/** Text of the newest user (human) bubble. */
export async function lastUserText(ui) {
  const element = await ui.last('[data-message-role="human"]');
  return element.getText();
}

/** Click a PrimeVue TabView header by its visible label. */
export async function clickTab(ui, label) {
  const clicked = await ui.driver.executeScript(
    `const want = arguments[0];
     const items = [...document.querySelectorAll('.p-tabview-tablist-item')];
     const target = items.find(item =>
       [...item.querySelectorAll('.p-tabview-tab-title')]
         .some(title => ((title.innerText || title.textContent || '') + '').trim() === want)
     ) || items.find(item => ((item.innerText || '') + '').trim() === want);
     if (!target) return false;
     const link = target.querySelector('a') || target;
     link.click();
     return true;`,
    String(label)
  );
  if (!clicked) {
    const found = await ui.driver.executeScript(
      `return {
         items: [...document.querySelectorAll('.p-tabview-tablist-item')].map(item => (item.innerText || '').trim()),
         owners: [...document.querySelectorAll('.p-tabview-tab-title')].map(e => (e.innerText || '').trim()),
       }`,
      String(label)
    );
    throw new Error(`tab header not found: ${label}; dom: ${JSON.stringify(found)}`);
  }
  await sleep(600);
}

/**
 * Bind the session's project directory.
 *
 * In the PACKAGED app the chip's "Choose folder…" opens the NATIVE OS dialog
 * (the Tauri branch of ProjectDirectoryChip.choose), which WebDriver cannot
 * drive. The binding itself is fixture state, not the behavior under test, so
 * the harness seeds it through the backend API; the file tree and the git
 * graph that READ the binding are still driven through the UI.
 */
export async function bindProjectDir(ui, sid) {
  const tokenResponse = await fetch('http://127.0.0.1:8080/auth/token', { method: 'GET' });
  const tokenPayload = await tokenResponse.json().catch(() => ({}));
  const token = tokenResponse.headers.get('token') || String(tokenPayload.token || '');
  if (!token) throw new Error('could not bootstrap the gateway token for fixture seeding');
  const response = await fetch('http://127.0.0.1:8080/sessions/project', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', token },
    body: JSON.stringify({ session_id: sid, directory: '/home/honor/sherry-e2e-workspace' })
  });
  if (!response.ok) {
    throw new Error(`project binding failed: ${response.status} ${await response.text()}`);
  }
  // The toolbar's folder button reads the FRONTEND store, which hydrates when
  // the chip's popover opens — open it (hydrate) and dismiss with a real
  // pointer press so the store picks up the API-side binding.
  // OPEN with a JS click (the trigger's Vue handler runs); only the DISMISS
  // needs a real pointer (outside-pointerdown).
  await ui.click('[data-test="project-dir-trigger"]');
  await ui.find('[data-test="project-dir-panel"]', 10_000);
  await ui.nativeClick('[data-test="sidebar-title-row"]');
  await sleep(400);
}

/** Text of the newest user (human) bubble. */
