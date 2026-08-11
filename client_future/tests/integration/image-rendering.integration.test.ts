import { describe, it, expect, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import ChatBox from '@/pages/home/components/ChatBox.vue';
import homeIndex from '@/pages/home/index.vue';
import { CHAT_ROLE, type MessageItem } from '@/pages/home/type';
import type { CachedMessage } from '@/composables/db';

/**
 * 图片渲染集成测试（backend mocked）。
 *
 * 覆盖本次媒体渲染修复的两条链路：
 *  1. ChatBox.vue 的 resolveImageSrc —— 把消息里的 `images` 转成可渲染的 <img src>：
 *       - 用户消息（base64，无 data: 前缀）→ `data:image/*;base64,<origin>`
 *       - AI 消息（持久化文件绝对路径）→ `{BACKEND}/media?session_id=<sid>&filename=<basename>`
 *  2. home/index.vue 的 toMessageItems —— 把服务器历史行(CachedMessage) 的 `images`
 *     透传给 ChatBox 的 messages prop。
 *
 * ChatBox 挂载真实 Vue 组件（非 stub），后端 URL 由 vitest.integration.config.ts 的
 * `VITE_API_BACK_URL=http://localhost:8080` 提供，正好覆盖 resolveImageSrc 的文件路径分支。
 */

const base = (over: Partial<MessageItem>): MessageItem => ({
  session_id: 'main',
  role: CHAT_ROLE.USER,
  content: 'hello',
  id: 1,
  turn_num: 0,
  timestamp: '20260621004725',
  ...over,
});

describe('ChatBox.vue image rendering (integration, backend mocked)', () => {
  it('renders a user base64 image with the data:image/*;base64, prefix', () => {
    const b64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [b64], content: '' })],
      },
    });
    // 图片容器内的 <img>（class w-24，区别于头像 w-full）
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    expect(img.attributes('src')).toBe(`data:image/*;base64,${b64}`);
  });

  it('renders an AI file-path image via the backend /media URL', () => {
    const wrapper = mount(ChatBox, {
      props: {
        messages: [
          base({
            id: 2,
            role: CHAT_ROLE.AI,
            session_id: 'main',
            images: ['C:/project/src/main/media/12345_67890.png'],
            // 注意：AI 消息 content 为空会被 filteredMessages 当作「空占位」过滤掉，
            // 进而连带图片分支也不渲染 —— 必须给非空内容才走正常渲染路径。
            content: 'look at this',
          }),
        ],
      },
    });
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    // backendBaseUrl 来自 VITE_API_BACK_URL=http://localhost:8080
    expect(img.attributes('src')).toBe(
      'http://localhost:8080/media?session_id=main&filename=12345_67890.png',
    );
  });

  it('renders no image container for a message without images', () => {
    const wrapper = mount(ChatBox, {
      props: { messages: [base({ id: 1, role: CHAT_ROLE.USER, content: 'plain' })] },
    });
    expect(wrapper.find('img.w-24').exists()).toBe(false);
  });

  it('renders multiple images in one message, each resolved independently', () => {
    const b1 = 'AAAA';
    const b2 = 'BBBB';
    const wrapper = mount(ChatBox, {
      props: {
        messages: [base({ id: 1, role: CHAT_ROLE.USER, images: [b1, b2], content: '' })],
      },
    });
    const imgs = wrapper.findAll('img.w-24');
    expect(imgs).toHaveLength(2);
    expect(imgs[0].attributes('src')).toBe(`data:image/*;base64,${b1}`);
    expect(imgs[1].attributes('src')).toBe(`data:image/*;base64,${b2}`);
  });
});

const primevueStub = {
  Checkbox: {
    name: 'Checkbox',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<button class="cb" @click="$emit(\'update:modelValue\', !modelValue)">C</button>',
  },
  Button: { props: ['label'], template: '<button class="btn"><slot /><span>{{ label }}</span></button>' },
  Menu: { template: '<div class="mnu"></div>', methods: { toggle() {} } },
  ToggleSwitch: { template: '<span class="ts"></span>' },
  ChatInputBox: { template: '<div class="cib"></div>' },
};

describe('home/index.vue image pass-through (integration, backend mocked)', () => {
  it('passes `images` from server history rows into ChatBox messages and renders them', async () => {
    // setup.ts 已把 get_history_by_turn_page stub 成空数组；这里在 mount 前覆盖
    // 返回带图片的历史行（toMessageItems 的输入是 CachedMessage）。
    const rows: CachedMessage[] = [
      {
        session_id: 'main',
        role: CHAT_ROLE.USER,
        content: 'with image',
        images: ['aGVsbG8='],
        id: 1,
        turn_num: 0,
        timestamp: '20260621004725',
        tool_call_id: null,
        tool_calls: null,
        tool_status: null,
        tool_name: null,
        finish_reason: null,
        reasoning: null,
        reasoning_content: null,
      },
    ];
    (globalThis as any).get_history_by_turn_page = vi.fn(async () => rows);

    const wrapper = mount(homeIndex, { global: { stubs: primevueStub } });
    // loadSessionHistory 是 async（模块顶层调用），等其 await 链路完成
    await flushPromises(); // get_history_by_turn_page 的 Promise + 合并写入 chatMessages
    await flushPromises();

    // ChatBox 是真实组件（未 stub）→ 透传后应真正渲染出 <img>
    const chatBox = wrapper.findComponent(ChatBox);
    expect(chatBox.exists()).toBe(true);
    const messages = chatBox.props('messages') as MessageItem[];
    expect(messages).toHaveLength(1);
    expect(messages[0].images).toEqual(['aGVsbG8=']);

    // 端到端：消息已透传并经 ChatBox.resolveImageSrc 渲染成 base64 data URL
    const img = wrapper.find('img.w-24');
    expect(img.exists()).toBe(true);
    expect(img.attributes('src')).toBe('data:image/*;base64,aGVsbG8=');
  });
});
