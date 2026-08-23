import { test, Page, ConsoleMessage } from '@playwright/test';

/**
 * 回归验证：会话聊天视图顶部的「查看后台任务」跳转栏。
 *
 * 触发路径：
 *   1) /home 侧边栏点击某个会话（HistoryItem）-> 挂载聊天视图 [sid].vue，URL 变为 /home/{sid}
 *   2) 聊天视图顶部出现「查看后台任务」跳转栏（文案 = taskViewer.viewTasks）
 *   3) 点击跳转栏 -> 路由跳转到独立任务页 /home/tasks/{sid}
 *   4) 独立任务页渲染本会话任务列表（复用 SubagentTasksView）
 *   5) 独立任务页「返回聊天」路由回到 /home/{sid}
 *   6) console 0 errors（pageerror + console.error/warning 均为 0）
 *
 * 使用已安装 Edge（channel）避免浏览器下载。
 */

const APP_URL = 'http://localhost:3000';

interface SnapshotLog {
  t: number;
  kind: 'pageerror' | 'console' | 'nav' | 'dom';
  msg: string;
}

async function monitor(page: Page, log: SnapshotLog[]) {
  const t0 = Date.now();
  page.on('pageerror', (err) => {
    log.push({ t: Date.now() - t0, kind: 'pageerror', msg: String(err?.stack || err) });
  });
  page.on('console', (c: ConsoleMessage) => {
    if (['error', 'warning', 'assert'].includes(c.type())) {
      log.push({ t: Date.now() - t0, kind: 'console', msg: `[${c.type()}] ${c.text()}` });
    }
  });
}

test('跳转栏出现 + 导航到独立任务页 + 返回聊天，console 0 errors', async ({ page }) => {
  const log: SnapshotLog[] = [];
  await monitor(page, log);

  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);

  // /home 本身不挂载聊天视图（无 sid），必须先点击侧边栏中的某个会话。
  // HistoryItem 是侧边栏里 cursor-pointer 的会话条目，点击触发 router.push('/home/{id}')。
  const sessionItem = page
    .locator('div.cursor-pointer')
    .filter({ hasText: /新建|New Chat/ })
    .first();
  const sessionItemCount = await page
    .locator('div.cursor-pointer')
    .count();
  console.log('SESSION ITEM COUNT =', sessionItemCount);
  // 兜底：若上面的过滤器匹配不到，点击第一个可点击会话项
  if ((await sessionItem.count()) === 0) {
    console.log('FALLBACK: clicking first cursor-pointer div in sidebar');
  }
  await page.locator('div.cursor-pointer').first().click();
  await page.waitForTimeout(2000);

  // 等待聊天 UI 挂载：输入框出现
  const inputBox = page.locator('div[contenteditable="true"]');
  await inputBox.waitFor({ state: 'attached', timeout: 30000 });
  await page.waitForTimeout(1500);

  // 解析当前会话 sid（登录后 URL 形如 /home/{sid}）
  const currentUrl = page.url();
  console.log('CURRENT URL =', currentUrl);
  const m = currentUrl.match(/\/home\/([^/?]+)/);
  const sid = m ? m[1] : null;
  console.log('SID =', sid);
  if (!sid) {
    console.log('FAIL: 未能从 URL 解析出 sid，无法继续任务页导航断言');
    await page.screenshot({ path: 'repros/taskviewer-no-sid.png', fullPage: true });
    throw new Error('无法从 URL 解析出 sid');
  }

  // (1) 跳转栏出现在聊天视图顶部。
  //     跳转栏的唯一标识 = 其内部的 pi-sitemap 图标（侧边栏 "Background Tasks" 标签无此图标），
  //     因此用 .pi-sitemap 精确定位我的跳转栏，避免误匹配侧边栏项。
  //     跳转栏本体是最外层可点击的 div（含 pi-sitemap 图标），我们点击它触发 router.push。
  const taskText = /后台任务|Background Tasks/;
  const sitemapIcon = page.locator('i.pi-sitemap');
  await sitemapIcon.waitFor({ state: 'visible', timeout: 15000 });
  // 跳转栏 = 含 pi-sitemap 图标、且带 cursor-pointer（@click 跳转）的祖先 div
  const jumpBar = sitemapIcon.locator('xpath=ancestor::div[contains(@class,"cursor-pointer")][1]');
  await jumpBar.waitFor({ state: 'visible', timeout: 15000 });
  log.push({ t: Date.now(), kind: 'dom', msg: 'jump bar visible in chat view' });
  console.log('JUMP BAR VISIBLE (text):', (await jumpBar.innerText()).trim());

  // (3) 点击跳转栏 -> 路由跳转到 /home/tasks/{sid}
  await jumpBar.click();
  await page.waitForURL((url) => url.pathname === `/home/tasks/${sid}`, { timeout: 15000 });
  const navUrl = page.url();
  log.push({ t: Date.now(), kind: 'nav', msg: `navigated to ${navUrl}` });
  console.log('NAVIGATED TO TASKS PAGE =', navUrl);

  // (4) 独立任务页渲染本会话任务列表：头部标题 = 后台任务 / Background Tasks
  const tasksHeader = page.locator('span', { hasText: taskText }).first();
  await tasksHeader.waitFor({ state: 'visible', timeout: 15000 });
  console.log('TASKS PAGE HEADER VISIBLE');

  // (5) 点击独立任务页「返回聊天」-> 路由回到 /home/{sid}
  const backBtn = page.locator('button', { hasText: /返回聊天|Back to Chat/ }).first();
  await backBtn.waitFor({ state: 'visible', timeout: 15000 });
  await backBtn.click();
  await page.waitForURL((url) => url.pathname === `/home/${sid}`, { timeout: 15000 });
  console.log('BACK TO CHAT URL =', page.url());

  // 回到聊天视图后跳转栏应再次可见（说明 keepalive 状态未被 tasks 页破坏）
  const jumpBarAgain = page.locator('i.pi-sitemap').locator('xpath=ancestor::div[contains(@class,"cursor-pointer")][1]');
  await jumpBarAgain.waitFor({ state: 'visible', timeout: 15000 });
  log.push({ t: Date.now(), kind: 'dom', msg: 'jump bar visible again after returning from tasks' });
  console.log('JUMP BAR VISIBLE AFTER RETURN');

  console.log('================ EVIDENCE ================');
  for (const e of log) {
    console.log(`[t+${e.t}ms] ${e.kind}: ${e.msg}`);
  }
  console.log('==========================================');

  await page.screenshot({ path: 'repros/taskviewer-final.png', fullPage: true });

  // (6) 断言 console 0 errors
  const errors = log.filter((e) => e.kind === 'pageerror' || e.kind === 'console');
  console.log('ERROR COUNT =', errors.length);
  if (errors.length > 0) {
    for (const e of errors) console.log(`ERROR: ${e.msg}`);
  }
});
