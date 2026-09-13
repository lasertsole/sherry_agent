/**
 * Backend health check.
 *
 * @module bridge/health
 */
import type { HealthStatus } from '~/types/backend/HealthStatus';
import { invokeNative } from './transport';
import { API_BASE_URL } from '../env';

/**
 * Check whether the Python backend is reachable.
 */
export async function checkHealth(): Promise<HealthStatus> {
  const native = await invokeNative<HealthStatus>('system_health');
  if (native !== null) return native.value;
  // Browser fallback: try to fetch a lightweight endpoint
  try {
    const resp = await fetch(`${API_BASE_URL}/system_prompt`);
    if (resp.ok) {
      return { healthy: true, message: 'Python backend reachable' };
    }
    return { healthy: false, message: `HTTP ${resp.status}` };
  } catch (e) {
    return { healthy: false, message: String(e) };
  }
}
