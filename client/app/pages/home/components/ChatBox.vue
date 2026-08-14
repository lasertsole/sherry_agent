<template>
  <div
    ref="scrollContainerRef"
    class="flex flex-col gap-6 flex-1 border-b border-solid border-gray-light dark:border-gray-dark overflow-auto px-6 py-4">
    <div
      v-for="group in turnGroups"
      :key="group[0].id"
      :class="['flex flex-col', { 'gap-3': turnSpacingClass(group) }]">
      <div
        v-for="message in group"
        :key="message.id"
        :class="[
          'flex justify-start gap-3',
          { 'flex-row-reverse text-right': message.role === CHAT_ROLE.USER },
          { 'text-left': message.role === CHAT_ROLE.AI }
        ]">
        <div class="flex justify-center items-center w-10 h-10 rounded-full overflow-hidden shrink-0 bg-gray-100 dark:bg-gray-800">
        <!-- 头像区域，连续消息与工具调用均不展示头像（隐藏 img，与连续消息渲染一致） -->
        <img
          v-if="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
          :class="['w-full h-full object-cover', { hidden: isConsecutive(message.id) || message.role === CHAT_ROLE.TOOL }]"
          :src="message.role === CHAT_ROLE.USER ? userAvatar : aiAvatar"
          :alt="message.role === CHAT_ROLE.USER ? resolvedUserName : resolvedAiName" />
        <span
          v-else
          :class="['pi pi-user', { hidden: isConsecutive(message.id) }]"></span>
      </div>
      <!-- 消息主体 -->
      <div :class="['flex flex-col max-w-[60%]', message.role === CHAT_ROLE.USER ? 'items-end' : 'items-start']">
        <!-- 用户/AI 时间 -->
        <div
          v-if="message.role !== CHAT_ROLE.TOOL"
          :class="[
            'flex items-center gap-2 mb-1',
            { 'text-right justify-end': message.role === CHAT_ROLE.USER },
            { 'text-left': message.role === CHAT_ROLE.AI }
          ]">
          <span class="text-sm font-semibold text-[#111827] dark:text-[#E5E7EB]">{{
            message.role === CHAT_ROLE.AI ? resolvedAiName : resolvedUserName
          }}</span>
          <span class="text-xs font-normal text-[#6B7280] dark:text-[#9CA3AF]">{{
            formatCompactTimeString(message.timestamp)
          }}</span>
        </div>
        <!-- 工具调用卡片 -->
        <div
          v-if="message.role === CHAT_ROLE.TOOL"
          class="flex items-center gap-2 px-3 py-2 rounded-lg border border-solid border-gray-200 dark:border-gray-600 bg-gray-50 dark:bg-gray-800/60 text-sm text-gray-600 dark:text-gray-300">
          <span class="pi pi-hammer text-xs"></span>
          <span class="font-medium">{{ message.toolName }}</span>
          <span
            v-if="message.toolStatus === 'running'"
            class="pi pi-spin pi-spinner text-xs text-blue-500"></span>
          <span
            v-else
            class="pi pi-check text-xs text-green-500"></span>
        </div>
        <!-- 对话内容气泡 -->
        <div
          v-else
          :class="[
            'w-fit p-3 text-sm font-normal leading-relaxed shadow-sm break-words transition-colors duration-200',
            message.role === CHAT_ROLE.USER
              ? 'bg-[#2563EB] text-[#FFFFFF] rounded-s-xl rounded-ee-xl dark:bg-[#3B82F6]' /* 右侧气泡：蓝色，左下角/右下角圆角定制 */
              : 'bg-white text-gray-900 rounded-e-xl rounded-es-xl border border-gray-100' /* 左侧气泡：白色 */,
            { 'rounded-xl': isConsecutive(message.id) }
          ]">
          <!-- eslint-disable-next-line vue/no-v-html -- safeHtml() 已内部净化（markdown-it + DOMPurify 白名单），此处 v-html 绑定的是净化后的安全 HTML -->
          <div v-html="safeHtml(message.content)"></div>
          <template v-if="messageImages(message).length">
            <div class="flex flex-wrap gap-2 mt-2">
              <template
                v-for="(src, i) in messageImages(message)"
                :key="i">
                <!-- 历史消息的媒体可能已在磁盘上不存在（media 特性落地前写入的行），
                     加载失败时隐藏破图占位并展示占位块，避免出现 broken image 图标。 -->
                <img
                  v-if="!failedImageSources.has(resolveImageSrc(message, src))"
                  :src="resolveImageSrc(message, src)"
                  class="w-24 h-24 object-cover rounded-lg border border-solid border-gray-200 cursor-pointer hover:opacity-80 transition-opacity duration-200"
                  @click="openPreview(resolveImageSrc(message, src))"
                  @error="onImageError($event, resolveImageSrc(message, src))" />
                <div
                  v-else
                  class="w-24 h-24 rounded-lg border border-dashed border-gray-300 dark:border-gray-600 flex items-center justify-center text-xs text-gray-400 dark:text-gray-500">
                  {{ t('chatBox.imageLoadFailed') }}
                </div>
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 组件

// 方法/类型
import type { MessageItem } from '../type';
import { CHAT_ROLE } from '../type';
import { formatCompactTimeString } from '@/common/utils';
import { useI18n } from 'vue-i18n';
import MarkdownIt from 'markdown-it';
import DOMPurify from 'dompurify';

const { t } = useI18n();

interface Props {
  messages: MessageItem[] | undefined;
  /** 用户头像 URL（服务端返回） */
  userAvatar?: string;
  /** AI 头像 URL（服务端返回） */
  aiAvatar?: string;
  /** 用户显示名（服务端返回） */
  userName?: string;
  /** AI 显示名（服务端返回） */
  aiName?: string;
}
const props = withDefaults(defineProps<Props>(), {
  messages: () => [] as MessageItem[],
  userAvatar: '',
  aiAvatar: '',
  userName: '',
  aiName: ''
});

/** 用户显示名：props 空时回退到 i18n 默认值 */
const resolvedUserName = computed(() => props.userName || t('chatBox.defaultUserName'));
/** AI 显示名：props 空时回退到 i18n 默认值 */
const resolvedAiName = computed(() => props.aiName || t('chatBox.defaultAiName'));

// 图片预览
const { openPreview } = useImagePreview();

/** 过滤tool后的消息列表 */
const isToolCallMsg = (msg: MessageItem) =>
  (msg as unknown as { tool_calls?: unknown[] }).tool_calls?.length;
const filteredMessages = computed(() => {
  return props.messages.filter((item: MessageItem) => {
    // 隐藏「AI 空占位」消息：发送后 AI 尚未产出任何内容（也无工具调用）时，
    // 不渲染这个只有名字+空白框的占位气泡，避免「橘雪莉」看起来贴在白框里。
    if (item.role === CHAT_ROLE.AI && !item.content.trim()) {
      return false;
    }
    return true;
  });
});

/**
 * 判断一条消息是否应渲染为「连续消息」（不显示头像、紧凑间距、直角紧贴气泡）。
 *
 * 必须在**原始（未过滤）**消息序列上判定，而不是在 `filteredMessages` 上：
 * `filteredMessages` 会把「AI 空占位」消息过滤掉，但空占位是一条真实的轮次边界
 * （handleSend 在每条用户消息后都会追加一个空的 AI 占位）。若在此过滤后的列表上按
 * 相邻同角色判定，会把 [用户A, AI空占位, 用户B] 中的 AI 占位剔除，使「用户B」误判为
 * 前一条「用户A」的连续消息 —— 这正是「打开页面第一次发送消息被当作连续消息」的根因。
 *
 * 正确语义：在原始序列里跳过 TOOL 行，只看紧邻的前一条可见消息是否同角色。空 AI 占位
 * 仍保有角色 `ai`，与用户消息不同角色，天然充当轮次分隔符；同一轮次内多条同角色行
 * （如一次 AI 回合内的工具调用 + 最终回复）仍能正确判为连续。
 */
const consecutiveIdSet = computed(() => {
  const result = new Set<number>();
  let prevRole: CHAT_ROLE | null = null;
  for (const item of props.messages) {
    if (item.role === CHAT_ROLE.TOOL) {
      continue;
    }
    if (prevRole === item.role) {
      result.add(item.id);
    }
    prevRole = item.role;
  }
  return result;
});
const isConsecutive = (id: number) => consecutiveIdSet.value.has(id);

/**
 * 将渲染消息按「回合」分组，实现分段间距：
 *  - 用户消息（USER）自成一组：前后都是回合边界，由外层 gap-6(24px) 提供疏离；
 *  - 首条消息后的连续 AI/TOOL 行归入同一组：组内用 gap-3(12px) 收紧
 *    （气泡↔工具卡片↔气泡紧凑贴合，含工具调用区间）。
 *
 * 这样相邻两行的间距即满足需求：只有「AI/TOOL → AI/TOOL」是 12px，
 * 任何涉 USER 的边界（user→AI、AI→user、user→user）都是外层 24px 的角色切换间距。
 *
 * 分组依据**渲染顺序**（filteredMessages）：空 AI 占位消息已被过滤，因此
 * `user → AI占位(过滤) → user` 会在渲染顺序上直接相邻，本行 user 正确开启新回合，
 * 避免下一条 AI 被误并入前一回合。
 */
const turnGroups = computed<MessageItem[][]>(() => {
  const groups: MessageItem[][] = [];
  for (const item of filteredMessages.value) {
    const last = groups.length ? groups[groups.length - 1] : null;
    const prevRole = last ? last[last.length - 1].role : null;
    // 新回合：首条、本行是 user（永远自成一组）、或上一行是 user（AI 回复与用户气泡分隔）
    if (item.role === CHAT_ROLE.USER || groups.length === 0 || prevRole === CHAT_ROLE.USER) {
      groups.push([item]);
    } else {
      // AI/TOOL 连续行 → 并入最后一个非用户分组
      groups[groups.length - 1].push(item);
    }
  }
  return groups;
});

/**
 * 判断某回合内是否应使用 12px 的内部间距（flex gap-3）。
 * 只有「多行且首行非 user」的组（纯 AI/TOOL 连续行）使用 gap-3；
 * 单行组或含 user 的组不需要，其上下间距由外层 gap-6(24px) 提供。
 */
const turnSpacingClass = (group: MessageItem[]): boolean =>
  group.length > 1 && group[0].role !== CHAT_ROLE.USER;

/** 聊天列表滚动容器（最外层 overflow-auto div），用于自动滚到底部 */
const scrollContainerRef = useTemplateRef<HTMLDivElement>('scrollContainerRef');

/**
 * 将聊天列表滚动到底部（新消息可见）。
 *
 * 必须在 DOM 更新后（nextTick）再取 scrollHeight，否则测量到的是旧高度，
 * 会导致滚不到最新消息底部。父组件 home/index.vue 在每次流式块到达时都会
 * 重新赋值 messages 数组（新引用），因此 watch 引用变化即可覆盖「首屏加载」、
 * 「发送消息」与「AI 每回复一块」三种场景。
 */
const scrollToBottom = () => {
  nextTick(() => {
    const el = scrollContainerRef.value;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  });
};

// 消息列表任一变化（含流式逐块追加）后均保持在底部
watch(() => props.messages, () => scrollToBottom());

// 组件挂载（首屏打开）后滚到底部，让最新消息可见
onMounted(() => scrollToBottom());

// 初始化 markdown-it
const md = new MarkdownIt({ html: true, linkify: true });

/** 后端 /media 端点根：从 VITE_API_BACK_URL 推导（去掉尾部斜杠） */
const backendBaseUrl = ((import.meta.env.VITE_API_BACK_URL as string) ?? '').replace(/\/+$/, '');

/**
 * 将消息里的图片条目解析为可渲染的 <img src>。
 * 语义（见 type.ts MessageItem.images 注释）：
 *  - 用户消息：原始 base64（不含 data: 前缀）→ 本地拼成 data:image/*;base64,<data>
 *  - AI 消息：持久化的绝对文件路径 → 走后端 /media，后端按 session_id + 文件名返回图片；
 *    需取原始 basename（如 <ts>.png），丢弃文件路径中的目录部分。
 * 判定依据：文件路径必然带反斜杠 \，或以常见媒体扩展名结尾；
 * 纯 base64 的字母表恰好含 / 与 +（且通常以 = 补位），
 * 因此绝不能把含 / 当作“文件路径”的判据——那会把用户原始
 * base64 图片误判成 /media 请求（历史 4 次“media not found”根因）。
 */
const resolveImageSrc = (message: MessageItem, entry: string): string => {
  const s = (entry ?? '').trim();
  if (!s) return '';
  // 绝对 URL（http/https）直接透传：中间件注入的用户图片是已服务
  // 出来的 http(s)://…/images/<hash>.png，必须以原样渲染，不能按扩展名
  // 误判成 /media 文件路径（否则 404 / 破图）。
  if (/^https?:\/\//i.test(s)) return s;
  const isFilePath =
    s.includes('\\') || /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(s);
  if (isFilePath) {
    // AI 消息：走 /media 拉取；文件可能带任意目录前缀，取其 basename
    const filename = s.split(/[\\/]/).pop() || '';
    return `${backendBaseUrl}/media?session_id=${encodeURIComponent(message.session_id ?? '')}&filename=${encodeURIComponent(filename)}`;
  }
  // 用户消息：本地 base64
  return `data:image/*;base64,${s}`;
};

/**
 * Extract image URLs injected by the multimodal processor into message content.
 *
 * The middleware persists a marker like:
 *   "[System: The user uploaded N image(s). Location: http://…/images/<hash>.png,…]"
 * This fallback resolves those served URLs so history/legacy rows whose
 * `images` field is empty still render their images.
 */
const extractContentImageUrls = (content: string): string[] => {
  if (!content) return [];
  const m = content.match(/Location:\s*([^\]\n]+)/);
  if (!m) return [];
  return m[1]
    .split(/[,\s]+/)
    .map((u) => u.replace(/[\]\s.,!;:]+$/g, ''))
    .filter((u) => /^https?:\/\//i.test(u));
};

/**
 * Images to render for a message: explicit `images` wins; otherwise fall back
 * to URLs parsed from the content's Location marker.
 */
const messageImages = (message: MessageItem): string[] => {
  const explicit = message.images ?? [];
  return explicit.length > 0 ? explicit : extractContentImageUrls(message.content);
};

/**
 * 已加载失败的图片 src 集合（如历史消息指向的 /media 文件在磁盘已不存在 → 404）。
 * 一旦某 src 加载失败即记录，后续重新渲染时不再尝试加载该 src，直接展示占位块。
 */
const failedImageSources = reactive(new Set<string>());

/** <img> 加载失败（含 404/网络错误）时的回调：把失败的 src 记入集合以隐藏破图。 */
const onImageError = (event: Event, src: string) => {
  if (src) {
    failedImageSources.add(src);
  }
};

/** 解析 MD 并进行 XSS 净化 */
const safeHtml = computed(() => (content: string) => {
  // 先把 markdown 转为原始 html 字符串
  const rawHtml = md.render(content);

  // 使用 DOMPurify 清理所有危险的标签（如 script）和属性（如 onerror）
  return DOMPurify.sanitize(rawHtml, {
    // 选填配置：如果你希望点击链接在新窗口打开，可以保留 target="_blank"
    ADD_ATTR: ['target']
  });
});
</script>
