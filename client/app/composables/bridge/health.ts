/**
 * Backend health check.
 *
 * @module bridge/health
 */
import type { HealthStatus } from '~/types/backend/HealthStatus';
import { invokeNative } from './transport';

/**
 * Check whether the Python backend is reachable.
 *
 * Probes through the shared API client's raw entry (`fetchApiRaw`): the same
 * base URL and token policy as the JSON calls, but every failure is mapped to
 * `{ healthy: false, message }` and no retry/toast is ever raised — a
 * reachability probe must stay silent so a background polling caller cannot
 * spam the user with error toasts.
 */
export async function checkHealth(): Promise<HealthStatus> {
  const native = await invokeNative<HealthStatus>('system_health');
  if (native !== null) return native.value;
  // Browser fallback: the dedicated process self-check. Public at BOTH gate
  // layers (gateway + login), so the probe stays truthful on the login page
  // and costs a fixed tiny payload — the old /system_prompt probe was neither.
  try {
    const resp = await fetchApiRaw({ url: '/health' });
    if (resp.ok) {
      return { healthy: true, message: 'Python backend reachable' };
    }
    return { healthy: false, message: `HTTP ${resp.status}` };
  } catch (e) {
    return { healthy: false, message: String(e) };
  }
}
