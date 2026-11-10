/**
 * Session socket wiring for one session page.
 *
 * The page installs the session-level socket handlers ONCE: every frame of the
 * persistent session socket (chunk / hitl_request / turn_started / queued /
 * queued_cancelled / queued_updated / send_now_ack / done / background task /
 * safety filter) is routed here regardless of which send produced it.
 */
import { useI18n } from 'vue-i18n';
import type { AgentSocket, AgentSocketHandlers, TurnStartedInfo } from './bridge/agent-socket-types';
import type { HitlRequestData } from '~/pages/home/type';

/** Every handler the page supplies verbatim (the slice owns the rest). */
type PassiveHandlers = Omit<AgentSocketHandlers, 'onHitl' | 'onSafetyFilter' | 'onTurnStarted'>;

/** Collaborators of the session socket wiring. */
export interface SessionSocketDeps {
  /** The session's ONE persistent agent socket. */
  socket: AgentSocket;
  /** Frame handlers routed straight through (chunk, queue, background, done). */
  handlers: PassiveHandlers;
  /** `hitl_request` -> the pending-approval card. */
  onHitl: (data: HitlRequestData) => void;
  /** `turn_started` -> the stream slice's turn bookkeeping. */
  onTurnStarted: (info: TurnStartedInfo) => void;
  /**
   * Runs right after `onTurnStarted` (the page refreshes the project-directory
   * chip here: a parked directory choice is promoted at the turn boundary).
   */
  afterTurnStarted?: (info: TurnStartedInfo) => void;
}

/**
 * Install the page's session-level socket handlers.
 * @param deps
 */
export function useSessionSocket(deps: SessionSocketDeps): void {
  const { t } = useI18n();

  deps.socket.setHandlers({
    ...deps.handlers,
    onHitl: deps.onHitl,
    onTurnStarted: info => {
      deps.onTurnStarted(info);
      deps.afterTurnStarted?.(info);
    },
    onSafetyFilter: info => {
      // A live-stream filter decision: a block is an error-level notice, a
      // replace / warn is informational. The transcript already reflects it.
      const text = t('safety.event', { count: info.ruleCount });
      if (info.action === 'block') toastError(t('safety.blocked'), text);
      else toastInfo(t('safety.replaced'), text);
    }
  });
}
