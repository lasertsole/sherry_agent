import { computed, ref } from 'vue';
import { readBackgroundConfig, saveBackground } from '@/composables/db';

/**
 * 聊天区背景图共享单例（模块级响应式状态）。
 *
 * 背景图是**全局**（非按会话）配置：用户在「系统设置 → 背景设置」上传，写入 Dexie 的
 * 全局唯一行（GLOBAL_SESSION_KEY）。为了让「保存后立即生效、无需刷新」，背景图状态必须
 * 是**真正的模块级单例**——`backgroundUrl`/`backgroundOpacity`/`backgroundLoaded` 声明在
 * 模块顶层（函数外），每次调用 `useChatBackground()` 都返回对**同一份** ref 的引用，
 * 而非各自的副本。
 *
 * 否则（若把 ref 声明在函数内）每次调用都会各自创建独立状态：`home/index.vue` 根容器绑定的
 * 与 `ConfigDialog.vue` 保存时修改的是两套互不相通的 ref，保存只会更新对话框自己的那份，
 * 根容器绑定不变 → 背景无法即时刷新，必须整页 reload 从 Dexie 重读才生效。这正是此前
 * 「保存后需刷新」bug 的根因。
 *
 * 注意：`colorMode` 必须在函数内调用（`useColorMode()` 依赖 Nuxt setup 上下文），因此
 * `chatBackgroundStyle`/`chatBackgroundOverlayStyle` 这两个 computed 也要留在函数内——
 * 它们读取的 `backgroundUrl`/`backgroundOpacity` 是模块级共享的，两处调用各自持有
 * computed，但都反射同一份 ref，故仍能一致、即时地响应式更新。
 *
 * - `loadBackground()`：幂等，首次调用从 Dexie 读取并填充单例状态（组件 onMounted 调用）。
 * - `setBackground(url, opacity)`：同步更新单例状态 + 持久化到 Dexie，供 ConfigDialog
 *   保存时调用（写入后所有共享该单例的组件立即响应，无需刷新）。
 * - `setBackgroundOpacity(opacity)`：仅更新透明度 + 持久化（保留现有背景图）。
 * - `chatBackgroundStyle`：响应式样式对象，**浅色/深色主题下均**在有背景图时返回
 *   background-image（深色主题同样展示照片）。
 * - `chatBackgroundOverlayStyle`：响应式遮罩样式。浅色主题下为白色遮罩、深色主题下为
 *   黑色遮罩，`opacity` = slider 值/100 —— 值越大照片越被冲淡成纯白/纯黑，直到完全遮蔽。
 */

// ── 模块级共享状态（真正的单例）──
// 所有 useChatBackground() 调用方共享这同一份 ref；setBackground 修改后全局即时生效。
const backgroundUrl = ref('');
const backgroundOpacity = ref(0);
const backgroundLoaded = ref(false);

/** 模块级幂等加载：首次读取 Dexie 填充单例状态 */
const loadBackground = async () => {
  if (backgroundLoaded.value) return;
  try {
    const cfg = await readBackgroundConfig();
    backgroundUrl.value = cfg?.backgroundUrl ?? '';
    backgroundOpacity.value = cfg?.backgroundOpacity ?? 0;
  } catch (e) {
    console.error('[useChatBackground] Failed to load background:', e);
  } finally {
    backgroundLoaded.value = true;
  }
};

/**
 * 模块级设置：同步更新共享单例状态 + 持久化到 Dexie。
 * 传入空字符串表示清除背景。持久化失败不抛出（前端本地缓存失败不应阻塞保存流程）。
 */
const setBackground = async (url: string, opacity: number = backgroundOpacity.value) => {
  backgroundUrl.value = url;
  backgroundOpacity.value = opacity;
  try {
    await saveBackground(url, opacity);
  } catch (e) {
    console.error('[useChatBackground] Failed to save background:', e);
  }
};

/** 模块级更新遮罩透明度（保留现有背景图），持久化到 Dexie */
const setBackgroundOpacity = async (opacity: number) => {
  backgroundOpacity.value = opacity;
  try {
    await saveBackground(backgroundUrl.value, opacity);
  } catch (e) {
    console.error('[useChatBackground] Failed to save background opacity:', e);
  }
};

export function useChatBackground() {
  const colorMode = useColorMode();

  /** 聊天区背景样式：浅色/深色主题下，有背景图时均应用 background-image */
  const chatBackgroundStyle = computed(() => {
    if (!backgroundUrl.value) return undefined;
    return {
      backgroundImage: `url("${backgroundUrl.value}")`,
      backgroundSize: 'cover',
      backgroundPosition: 'center',
      backgroundRepeat: 'no-repeat'
    };
  });

  /** 聊天区遮罩样式：浅色=白色 / 深色=黑色，opacity 随 slider 递增，「越满越白/越黑直至遮蔽」 */
  const chatBackgroundOverlayStyle = computed(() => {
    const overlayColor = colorMode.value === 'light' ? '#ffffff' : '#000000';
    return {
      backgroundColor: overlayColor,
      opacity: backgroundOpacity.value / 100
    };
  });

  return {
    backgroundUrl,
    backgroundOpacity,
    backgroundLoaded,
    loadBackground,
    setBackground,
    setBackgroundOpacity,
    chatBackgroundStyle,
    chatBackgroundOverlayStyle
  };
}
