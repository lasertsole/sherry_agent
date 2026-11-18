/**
 * Small WebDriver facade used by every feature stub.
 *
 * Only user-visible actions are exposed (click, type, press Enter, read text).
 * Tests never call the frontend store or fetch APIs directly.
 */
import fs from 'node:fs';
import path from 'node:path';
import { By, Key, sleep, until } from './driver.mjs';

function cssString(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

export class Ui {
  /**
   * @param {import('selenium-webdriver').WebDriver} driver
   * @param {string} reportDir
   */
  constructor(driver, reportDir) {
    this.driver = driver;
    this.reportDir = reportDir;
    this.timeout = 30_000;
  }

  async find(css, timeout = this.timeout) {
    const element = await this.driver.wait(until.elementLocated(By.css(css)), timeout, `element not found: ${css}`);
    await this.driver.wait(until.elementIsVisible(element), timeout, `element not visible: ${css}`);
    return element;
  }

  async findAll(css) {
    return this.driver.findElements(By.css(css));
  }

  async last(css, timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const elements = await this.findAll(css);
      if (elements.length) return elements[elements.length - 1];
      await sleep(200);
    }
    throw new Error(`no elements matched: ${css}`);
  }

  async click(css) {
    const element = await this.find(css);
    // WebKitWebDriver's native click can silently miss Vue's handler when a
    // transparent overlay owns the coordinates; a DOM click is the same
    // user-visible action and works across WebKitGTK versions.
    await this.driver.executeScript('arguments[0].click()', element);
    return element;
  }

  async clickText(text, tag = 'button') {
    const xpath = `//${tag}[normalize-space(.)=${JSON.stringify(String(text))}]`;
    const element = await this.driver.wait(
      until.elementLocated(By.xpath(xpath)),
      this.timeout,
      `clickable text not found: ${text}`
    );
    await this.driver.executeScript('arguments[0].click()', element);
    return element;
  }

  async clickAriaPrefix(prefix) {
    return this.click(`[aria-label^="${cssString(prefix)}"]`);
  }

  async type(css, text) {
    const element = await this.find(css);
    // WebKitWebDriver's sendKeys can return before every key event has reached
    // Vue, so a following click sends a truncated prompt. Insert the full text
    // as one input event instead; clicks/Enter still use the real controls.
    await this.driver.executeScript(
      `const el = arguments[0], text = arguments[1];
       el.focus();
       el.textContent = text;
       el.dispatchEvent(new InputEvent('input', {
         bubbles: true, cancelable: true, inputType: 'insertText', data: text
       }));`,
      element,
      String(text)
    );
    await new Promise(resolve => setTimeout(resolve, 150));
    return element;
  }

  async pressEnter(css) {
    const element = await this.find(css);
    await element.sendKeys(Key.ENTER);
  }

  async text(css) {
    return (await this.find(css)).getText();
  }

  async bodyText() {
    return (await this.find('body')).getText();
  }

  async exists(css) {
    try {
      const elements = await this.findAll(css);
      return elements.length > 0;
    } catch {
      return false;
    }
  }

  async waitText(needle, timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const text = await this.bodyText();
      if (text.includes(needle)) return text;
      await sleep(250);
    }
    throw new Error(`body text never contained: ${needle}`);
  }

  async waitNoText(needle, timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const text = await this.bodyText();
      if (!text.includes(needle)) return text;
      await sleep(250);
    }
    throw new Error(`body text still contained: ${needle}`);
  }

  async waitGone(css, timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const elements = await this.findAll(css);
      let visible = false;
      for (const element of elements) {
        try {
          if (await element.isDisplayed()) {
            visible = true;
            break;
          }
        } catch {
          // Detached while polling.
        }
      }
      if (!visible) return;
      await sleep(200);
    }
    throw new Error(`element stayed visible: ${css}`);
  }

  async clickIfPresent(css) {
    if (!(await this.exists(css))) return false;
    try {
      await this.click(css);
      return true;
    } catch {
      return false;
    }
  }

  async pathname() {
    return this.driver.executeScript('return window.location.pathname');
  }

  async waitForSid(timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const pathname = await this.pathname();
      const match = String(pathname).match(/\/home\/([^/?#]+)/);
      if (match && match[1] !== 'home') return decodeURIComponent(match[1]);
      await sleep(250);
    }
    throw new Error('URL never reached /home/<session-id>');
  }

  /** Wait for a session URL that differs from `previousSid` (new-session race). */
  async waitForNewSid(previousSid, timeout = this.timeout) {
    const started = Date.now();
    while (Date.now() - started < timeout) {
      const pathname = await this.pathname();
      const match = String(pathname).match(/\/home\/([^/?#]+)/);
      if (match && match[1] !== 'home' && match[1] !== previousSid) {
        return decodeURIComponent(match[1]);
      }
      await sleep(250);
    }
    throw new Error(`URL never changed to a new session (previous=${previousSid})`);
  }

  async execute(script, ...args) {
    return this.driver.executeScript(script, ...args);
  }

  async screenshot(name) {
    fs.mkdirSync(this.reportDir, { recursive: true });
    const target = path.join(this.reportDir, `${name}.png`);
    const encoded = await this.driver.takeScreenshot();
    fs.writeFileSync(target, Buffer.from(encoded, 'base64'));
    return target;
  }
}
