---
name: browser
description: Drive the session's real browser (the 工具箱·浏览器 panel) — open pages, read them as text, click and type, screenshot when layout matters. Use it for any task that needs a LIVE web page instead of web_search snippets.
scope: main_only
---

# Browser

The session's browser is a real Chromium page, shown in the right sidebar's
工具箱·浏览器 panel. **The user sees the same page and can take over at any
moment** — your clicks and theirs land on the same tab. Treat it like a pair
programming session: act in small, verifiable steps.

The tool family: `browser_navigate` · `browser_snapshot` · `browser_click` ·
`browser_type` · `browser_press` · `browser_scroll` · `browser_screenshot`
(+ `browser_evaluate`, present only when the escape hatch is enabled).

## The loop that works: snapshot → act → verify

1. `browser_navigate(url)` — opens the page (the first call creates it).
2. `browser_snapshot()` — read the page: its text, plus interactive elements
   numbered `[e1] [e2] …`. **This is how you see the page.**
3. Act on a ref: `browser_click(ref="e5")`, `browser_type(text=…, ref="e3")`.
4. After any page-changing action, snapshot again before deciding the next
   step — do not act twice on one snapshot.

## Rules that save the most time

- **Refs die on navigation** (and often after a click that reloads or reroutes).
  An `unknown ref` error means exactly one thing: take a fresh snapshot.
- **Text beats screenshots.** The snapshot's text is the readable page content
  and its element list gives you the handles; screenshots cost far more tokens
  and are for layout/visual questions only ("does it overlap", "what does the
  icon look like"). When you do need to look at one:
  `browser_screenshot()` returns a PNG path — view it with the `image_to_text`
  skill (run its script via the `terminal` tool).
- **Scroll, don't demand.** Long pages are truncated in the snapshot; scroll
  (`browser_scroll(delta_y=600)`) and snapshot again rather than retrying with
  a huge `max_elements`.
- **Search boxes**: `browser_type(text="…", ref="e2", submit=True)` presses
  Enter for you.
- **Forms**: type into refs in order, then click the submit ref and snapshot to
  confirm what happened (a validation error is a normal outcome).
- **Cookies/login walls are the user's calls.** When a page demands credentials
  or a CAPTCHA, stop and ask (the `question` tool) — the user can take the page
  over in the panel and log in themselves; their session persists in the
  browser profile.
- **Multi-step flows**: one snapshot, one action, one verify. If two actions
  need the same knowledge, you are missing a snapshot in between.

## Escape hatch (`browser_evaluate`, when enabled)

For what the typed tools cannot express: extracting a table as JSON, reading a
computed value, scrolling inside an inner container. It runs in the page and
can change it — same caution as clicking. Prefer a snapshot when it suffices.

## When this feature is off

The tools above simply do not exist (the feature is opt-in and off by default).
Do not pretend to browse: say the browser feature is disabled and that turning
it on is `SHERRY_BROWSER_AGENT_ENABLED=1` plus a server restart.
