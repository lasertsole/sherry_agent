# User Login (account protection)

**English** · [中文](README.zh.md) · [日本語](README.ja.md) · [한국어](README.ko.md)

Sherry ships with **no login**: a fresh install has no account and login
protection is off, so the desktop app and a local browser go straight in. Set up
an account and remote access is gated; the local machine stays exempt.

## Enable or disable

1. Open the app and go to the settings menu (the nine-grid) → **Account**.
2. Enter a username, a password (at least 8 characters) and its confirmation,
   then press **Enable login protection**. That creates the account and turns
   protection on in one step.
3. From then on, access from another machine asks for that username and
   password; this machine keeps working without a login.

Turning protection off (or back on) requires the **current password** — the
account itself is kept, so re-enabling never needs a new password. Change the
username or the password from the same panel with the current password.

## What is protected

Everything except the login flow itself and the static assets of the app shell,
so the login page can load. The gate answers `401` for a request without a valid
session; the client then shows the login page and replays the request after a
successful login.

## How the session works

* The password is stored as a salted **scrypt** hash; the plaintext never
  touches disk.
* Login hands out two **HttpOnly cookies** (an access token scoped to the whole
  API and a refresh token scoped to the refresh endpoint). No token is ever
  visible to JavaScript, so a script injection cannot read the session.
* The access cookie lasts 12 hours; the client rotates it shortly before expiry,
  and every rotation invalidates the previous refresh token — a stolen copy is
  worth at most one use.
* Logging out revokes both tokens immediately.

## Local network and remote access

The exemption is decided by the **socket address**, never by a header, so it
cannot be spoofed. To require a login even on this machine, set
`SHERRY_AUTH_REQUIRE_NON_LOOPBACK=0`. To protect a deployment from its first
boot, set `SHERRY_AUTH_ENABLED=1` and pin `SHERRY_AUTH_SECRET` (otherwise every
restart invalidates existing sessions).

## Troubleshooting

* **Locked out remotely?** Use the machine itself: the account menu is reachable
  locally without a login (unless that exemption is disabled).
* **Every write fails with a 401 after a restart?** `SHERRY_AUTH_SECRET` is
  empty, so the signing key changed with the process. Pin it.
* **Cookies not arriving?** The browser needs the API on the same site as the
  page (or the origin must be in `SHERRY_ALLOWED_ORIGINS`); check that the
  frontend is opened through the same host name the API uses.
