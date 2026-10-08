/**
 * Global login guard, registered from a plugin.
 *
 * Nuxt's own spelling for a global route middleware is `middleware/*.global.ts`,
 * which the repo's naming hook forbids; a plugin can register exactly the same
 * thing with `addRouteMiddleware(..., { global: true })` and stays kebab-case.
 * The handler is exported so the guard's decisions are unit-testable without
 * Nuxt.
 *
 * The decision comes from the backend (`/auth/status`), never from a token in
 * JS: `auth_required` is true only when protection is on, an account exists and
 * this client is not exempt. A loopback browser (the desktop app, a local dev
 * session) therefore never sees the login page.
 *
 * `/login` is the only public page: reaching it while signed in (or while no
 * login is required at all) bounces to /home so a stale bookmark cannot strand
 * the user on a form that has nothing to do.
 */

import type { RouteMiddleware } from '#app';

export const authGuard: RouteMiddleware = async to => {
  const auth = useAuthStore();
  await auth.checkAuthStatus();

  const onLoginPage = to.path === '/login';

  if (!auth.authRequired) {
    // Nothing enforces a login for this client: /login is pointless.
    if (onLoginPage && to.query?.force !== '1') return navigateTo('/home');
    return;
  }

  if (onLoginPage) {
    // Enforcement is on: a live session going to /login means "already in".
    if (auth.user) return navigateTo('/home');
    const current = await auth.fetchUser();
    if (current) return navigateTo('/home');
    return;
  }

  // Enforcement is on and the user is not signed in: try the cookie session
  // once (a page reload has no store state yet), then ask for the password.
  if (!auth.user) {
    const current = await auth.fetchUser();
    if (!current) return navigateTo('/login');
  }
};

export default defineNuxtPlugin(() => {
  addRouteMiddleware('auth-guard', authGuard, { global: true });
});
