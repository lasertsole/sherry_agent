/**
 * WebSocket 连接管理与消息监听
 *
 * WebSocket 连接管理
 * - 创建并保持 WebSocket 连接
 * - 后台持续监听消息，通过 mitt 事件总线分发事件
 *
 * @module ws
 */

import { ref, type Ref } from 'vue';
import { emit } from './mitt';

/** 会话 ID（当前固定为 "default"） */
const SESSION_ID = 'default';

/** WebSocket 单例引用 */
let wsInstance: WebSocket | null = null;

/**
 * 解析 ws:// 或 wss:// URL 中的 host 与 port 部分
 *
 * 从 VITE_API_BACK_URL（如 http://localhost:8080）中提取 host 和 port，
 * 构建对应的 WebSocket URL。
 *
 * @param apiBaseUrl HTTP 基地址
 * @returns WebSocket 基地址 (ws://host:port)
 */
function resolveWsBaseUrl(apiBaseUrl: string): string {
  // 替换协议: http:// -> ws://, https:// -> wss://
  const wsUrl = apiBaseUrl.replace(/^https?:\/\//, (match) =>
    match === 'http://' ? 'ws://' : 'wss://',
  );
  // 去掉末尾的 /
  return wsUrl.replace(/\/+$/, '');
}

/**
 * 创建并获取 WebSocket 连接（单例）
 *
 * 使用 @st.cache_resource 的等效方案：模块级单例 + 连接状态管理
 *
 * @param {{ onReconnect?: () => void }} [options] 可选的连接恢复回调
 * @returns {{ ws: Ref<WebSocket | null>, isConnected: Ref<boolean> }}
 */
export function useWs(options?: { onReconnect?: () => void }): {
  ws: Ref<WebSocket | null>;
  isConnected: Ref<boolean>;
} {
  const ws: Ref<WebSocket | null> = ref(null);
  const isConnected: Ref<boolean> = ref(false);

  if (wsInstance && wsInstance.readyState === WebSocket.OPEN) {
    ws.value = wsInstance;
    isConnected.value = true;
    return { ws, isConnected };
  }

  const baseUrl = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const wsBase = resolveWsBaseUrl(baseUrl);
  const wsUrl = `${wsBase}/sessions/ws?session_id=${SESSION_ID}`;

  function connect(): void {
    // 关闭旧连接
    if (wsInstance) {
      wsInstance.close();
      wsInstance = null;
    }

    const socket = new WebSocket(wsUrl);
    wsInstance = socket;
    ws.value = socket;

    socket.onopen = () => {
      isConnected.value = true;
      emit('ws:connected', undefined);
    };

    socket.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        const eventType: string = data.event ?? '';
        const content: unknown = data.content ?? '';

        if (eventType === 'notification') {
          // 分发通知事件，供组件监听
          emit('ws:notification', content);
        }

        // 透传原始事件
        emit('ws:message', data);
      } catch {
        // JSON 解析失败，忽略该消息
      }
    };

    socket.onclose = () => {
      isConnected.value = false;
      ws.value = null;
      wsInstance = null;
      emit('ws:disconnected', undefined);

      // 自动重连（5 秒后）
      setTimeout(() => {
        options?.onReconnect?.();
        connect();
      }, 5000);
    };

    socket.onerror = () => {
      // onclose 会在 onerror 后自动触发，重连逻辑由 onclose 处理
    };
  }

  connect();

  return { ws, isConnected };
}

/**
 * 手动关闭 WebSocket 连接（清理用）
 */
export function closeWs(): void {
  if (wsInstance) {
    wsInstance.close();
    wsInstance = null;
  }
}

/* ---------------------------------------------------------------------------
 * 子任务（subagent）实时推送 WebSocket（/subagents/ws）
 *
 * 与上面的 `/sessions/ws` 会话推送通道相互独立的一段连接。后端在子任务
 * 被派生（spawned）/ 结束（ended）时，通过 `subagent_spawned` /
 * `subagent_ended` 两个 wire 事件把该子任务的运行记录推送给前端；
 * 连接建立后服务端会先发一帧 `ready` 欢迎帧。
 *
 * 前端收到事件后把每帧的 `data`（含 run_id 的完整记录）写到 IndexedDB，
 * 后台任务列表即可以 Dexie 为权威数据源实时更新。同样支持断线自动重连。
 * ------------------------------------------------------------------------- */

/** 子任务 WebSocket 单例引用 */
let subagentWsInstance: WebSocket | null = null;

/** 子任务连接是否已就绪（收到 ready 帧） */
let subagentReady = false;

/**
 * 创建并获取子任务实时推送 WebSocket 连接（单例）
 *
 * @param {{ onReconnect?: () => void }} [options] 可选的连接恢复回调
 * @returns {{ ws: Ref<WebSocket | null>, isConnected: Ref<boolean>, isReady: Ref<boolean> }}
 */
export function useSubagentWs(options?: { onReconnect?: () => void }): {
  ws: Ref<WebSocket | null>;
  isConnected: Ref<boolean>;
  isReady: Ref<boolean>;
} {
  const ws: Ref<WebSocket | null> = ref(null);
  const isConnected: Ref<boolean> = ref(false);
  const isReady: Ref<boolean> = ref(false);

  if (subagentWsInstance && subagentWsInstance.readyState === WebSocket.OPEN) {
    ws.value = subagentWsInstance;
    isConnected.value = true;
    isReady.value = subagentReady;
    return { ws, isConnected, isReady };
  }

  const baseUrl = import.meta.env.VITE_API_BACK_URL || 'http://localhost:8080';
  const wsBase = resolveWsBaseUrl(baseUrl);
  const wsUrl = `${wsBase}/subagents/ws`;

  function connect(): void {
    // 关闭旧连接
    if (subagentWsInstance) {
      subagentWsInstance.close();
      subagentWsInstance = null;
    }

    const socket = new WebSocket(wsUrl);
    subagentWsInstance = socket;
    subagentReady = false;
    ws.value = socket;

    socket.onopen = () => {
      isConnected.value = true;
      emit('ws:subagents:connected', undefined);
    };

    socket.onmessage = (event: MessageEvent) => {
      try {
        const data = JSON.parse(event.data);
        const eventType: string = data.event ?? '';
        const eventData: unknown = data.data ?? null;

        if (eventType === 'ready') {
          subagentReady = true;
          isReady.value = true;
          emit('ws:subagents:ready', eventData);
          return;
        }

        if (eventType === 'subagent_spawned') {
          emit('ws:subagent_spawned', eventData);
        } else if (eventType === 'subagent_ended') {
          emit('ws:subagent_ended', eventData);
        }

        // 透传原始事件事件
        emit('ws:subagents:message', data);
      } catch {
        // JSON 解析失败，忽略该消息
      }
    };

    socket.onclose = () => {
      subagentReady = false;
      isConnected.value = false;
      isReady.value = false;
      ws.value = null;
      subagentWsInstance = null;
      emit('ws:subagents:disconnected', undefined);

      // 自动重连（5 秒后）
      setTimeout(() => {
        options?.onReconnect?.();
        connect();
      }, 5000);
    };

    socket.onerror = () => {
      // onclose 会在 onerror 后自动触发，重连逻辑由 onclose 处理
    };
  }

  connect();

  return { ws, isConnected, isReady };
}

/**
 * 手动关闭子任务 WebSocket 连接（清理用）
 */
export function closeSubagentWs(): void {
  if (subagentWsInstance) {
    subagentWsInstance.close();
    subagentWsInstance = null;
  }
  subagentReady = false;
}
