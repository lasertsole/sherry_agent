/**
 * Unified communication bridge between the Nuxt frontend and the backend.
 *
 * Facade module: the implementation lives in the per-domain `bridge/*` modules
 * (chat streaming, session/subagents, prompts, memory, heartbeat, cron, skills,
 * curator, channels, logs, health + the transport infrastructure). Nuxt only
 * auto-imports the top level of `composables/`, so every public symbol is
 * re-exported here explicitly and consumers keep importing from `~/composables/bridge`.
 *
 * Supports two runtime modes:
 * - **Tauri desktop**: IPC via `invoke()` + Tauri Events for streaming
 * - **Browser dev**: direct HTTP to the Python backend via `fetchApi()`
 *
 * All API calls go through this module so that components never
 * need to know which transport layer is active.
 *
 * @module bridge
 */

// ── Chat protocol types & constants ─────────────────────
export type {
  ChatRequest,
  AgentWsEventType,
  AgentChunkType,
  HitlInterruptData,
  HitlResponse,
  AgentWsEvent,
  OnChunkCallback,
  OnHitlCallback,
  QueuedInfo,
  OnQueuedCallback,
  OnDoneCallback,
  StreamController
} from './bridge/chat-types';
export {
  StreamInterruptedError,
  WS_RECONNECT_MAX_ATTEMPTS,
  wsReconnectDelayMs,
  WS_CONN_LOSS_EVENT
} from './bridge/chat-types';

// ── Chat streaming & transport ──────────────────────────
export type { UploadMediaKind } from './bridge/upload';
export type { ChatStreamCallbacks, TransportStrategy } from './bridge/chat';
export { TauriTransport, BrowserTransport, createTransport } from './bridge/chat';
export { sendChatMessage, streamChatMessage, stopChatMessage } from './bridge/chat';
export { sendChatMessageWs } from './bridge/ws-stream';
export { resumeHitl } from './bridge/chat-resume';

// ── Session & subagents ─────────────────────────────────
export type { SubagentRun } from './bridge/session';
export {
  clearSession,
  fetchSubagentRuns,
  fetchSubagentRunSubtree,
  deleteSubagentRunSubtree,
  steerSubagentRun,
  getHistory
} from './bridge/session';

// ── System prompt ───────────────────────────────────────
export {
  readSystemPrompt,
  readSystemPromptTemplate,
  writeSystemPrompt,
  updateSystemPrompt
} from './bridge/system-prompt';

// ── Memory & heartbeat ──────────────────────────────────
export { readMemory, writeMemory, readHeartbeat, writeHeartbeat } from './bridge/memory';

// ── Cron (scheduled tasks) ──────────────────────────────
export type {
  CronSchedule,
  CronPayload,
  CronJobState,
  CronJob,
  CronListResponse,
  CronMutateResponse,
  CronActionResponse
} from './bridge/cron';
export { listCronJobs, addCronJob, updateCronJob, runCronJob, enableCronJob, deleteCronJob } from './bridge/cron';

// ── Skills ──────────────────────────────────────────────
export type { SkillInfo, SkillFileNode, SkillDetail, DeleteSkillResponse, PinSkillResponse } from './bridge/skills';
export { listSkills, readSkill, uploadSkill, setSkillActive, deleteSkill, pinSkill } from './bridge/skills';

// ── Curator ─────────────────────────────────────────────
export type {
  CuratorAutoTransitions,
  CuratorRunResult,
  CuratorRunResponse,
  CuratorSettings,
  CuratorSettingsUpdateResponse
} from './bridge/curator';
export { runCuratorReview, getCuratorSettings, setCuratorSettings } from './bridge/curator';

// ── Channels ────────────────────────────────────────────
export type { ChannelInfo, ChannelUpdate, ChannelConfig, ChannelConfigResponse } from './bridge/channels';
export { listChannels, updateChannel, getChannelConfig, updateChannelConfig } from './bridge/channels';

// ── Logs ────────────────────────────────────────────────
export type { LogLevel, LogFileInfo, LogFileList, LogReadResult, LogStreamData, LogStreamFrame } from './bridge/logs';
export { listLogFiles, readLogFile, openLogStream } from './bridge/logs';

// ── Health ──────────────────────────────────────────────
export { checkHealth } from './bridge/health';
