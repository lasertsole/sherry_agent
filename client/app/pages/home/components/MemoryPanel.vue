<template>
  <!-- Right-sidebar tab body: mounted and unmounted with its tab (the sidebar owns the
       tab label and the close button), so the mount drives the load. -->
  <div class="flex flex-col gap-3 h-full min-h-0 p-4">
    <div
      v-if="loading"
      class="flex items-center justify-center py-8">
      <ProgressSpinner style="width: 2rem; height: 2rem" />
    </div>
    <template v-else>
      <TabView
        v-model:activeIndex="activeTab"
        class="flex-1 min-h-0">
        <TabPanel
          v-for="tab in tabs"
          :key="tab.file"
          :value="tab.file"
          :header="t(tab.i18nKey)">
          <div class="flex flex-col gap-2">
            <div class="flex items-center justify-between">
              <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
              <span
                :class="[
                  'text-xs',
                  joinedLength(tab.file) > MAX_CHARS
                    ? 'text-red-500'
                    : joinedLength(tab.file) > MAX_CHARS * 0.9
                      ? 'text-orange-500'
                      : 'text-gray-400'
                ]">
                {{ joinedLength(tab.file) }} / {{ MAX_CHARS }}
              </span>
            </div>

            <div class="text-xs text-yellow-500 dark:text-yellow-400">
              {{ t('config.memory.effectiveHint') }}
            </div>

            <div
              v-if="!entries(tab.file).length"
              class="text-sm text-gray-400 dark:text-gray-500 pb-2">
              {{ t('config.memory.empty') }}
            </div>

            <div
              v-for="(entry, idx) in entries(tab.file)"
              :key="idx"
              class="flex flex-col gap-1 rounded-lg border border-gray-200 dark:border-gray-700 p-2">
              <div class="flex items-center justify-between">
                <span class="text-xs text-gray-400 dark:text-gray-500">
                  #{{ idx + 1 }} · {{ entryLength(tab.file, idx) }} chars
                </span>
                <Button
                  icon="pi pi-trash"
                  text
                  severity="danger"
                  :aria-label="t('config.memory.deleteEntry')"
                  class="!w-8 !h-8 !p-0"
                  @click="removeEntry(tab.file, idx)" />
              </div>
              <Textarea
                :model-value="entries(tab.file)[idx]"
                rows="3"
                class="w-full font-mono text-sm"
                autoResize
                style="min-height: 4.5rem; max-height: 24rem"
                @update:model-value="setEntry(tab.file, idx, $event)" />
            </div>

            <Button
              :label="t('config.memory.addEntry')"
              icon="pi pi-plus"
              text
              severity="secondary"
              class="self-start"
              @click="addEntry(tab.file)" />
          </div>
        </TabPanel>
      </TabView>
    </template>
    <!-- The removed dialog footer's save action; closing is the sidebar tab's × now.
         Pinned to the panel's bottom-right: the tab body above it is what scrolls. -->
    <div class="shrink-0 flex gap-2 justify-end">
      <Button
        :label="t('config.save')"
        icon="pi pi-check"
        :loading="saving"
        :disabled="!canSave"
        @click="handleSave" />
    </div>
  </div>
</template>

<script lang="ts" setup>
import { ref, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import { logUtil } from '~/utils/log';

/**
 * Memory files are stored on disk as a header line + a list of entries
 * delimited by "\n§\n" (see agent/tools/memory.py ENTRY_DELIMITER). Each
 * entry may itself contain multiple lines. The raw file's first line is an
 * H1 title ("# MEMORY" / "# USER") that acts as a file header — it is NOT a
 * memory entry. The backend (server/service/memory.py) returns the whole
 * file verbatim; the frontend must split entries while treating the header
 * as metadata, and re-attach it when persisting.
 */
const ENTRY_DELIMITER = '\n§\n';

/**
 * Derive the H1 title used as a file header (e.g. 'MEMORY.md' -> '# MEMORY').
 * Only the exact title of the current file is treated as a header.
 * @param file
 */
function fileTitle(file: string): string {
  return `# ${file.replace(/\.md$/i, '')}`;
}

/**
 * Re-assemble a full raw file body for a given file: header line, then the
 * entries joined by the on-disk delimiter. Mirrors the on-disk layout the
 * backend writes and expects to read back.
 * @param file
 * @param entries
 */
function joinFileBody(file: string, entries: string[]): string {
  const body = entries.filter(e => e.trim().length > 0).join(ENTRY_DELIMITER);
  if (!body) return fileTitle(file);
  return `${fileTitle(file)}${ENTRY_DELIMITER}${body}`;
}

const { t } = useI18n({ useScope: 'local' });

const emits = defineEmits<{ saved: [] }>();

// Whole-file character budget. Mirrors server/service/memory.py write_memory_files,
// which rejects len(content) > 8_000 on the joined raw file.
const MAX_CHARS = 8000;

interface MemoryTab {
  /** Actual filename under workspace/memory/ (e.g. 'MEMORY.md'). */
  file: string;
  i18nKey: string;
  i18nDescKey: string;
}

const tabs: MemoryTab[] = [
  { file: 'MEMORY.md', i18nKey: 'config.tabs.memoryAgent', i18nDescKey: 'config.desc.memoryAgent' },
  { file: 'USER.md', i18nKey: 'config.tabs.memoryUser', i18nDescKey: 'config.desc.memoryUser' }
] as const;

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
/** Live editable entries per file (array copy so v-model indexes stay stable). */
const editEntries = ref<Record<string, string[]>>({});
/** Frozen entries per file at load time, for dirty tracking. */
const originalEntries = ref<Record<string, string[]>>({});

/**
 * Split a raw memory file into entries using the on-disk delimiter.
 * @param file
 * @param raw
 */
function splitEntries(file: string, raw: string): string[] {
  if (!raw || !raw.trim()) return [];

  const header = fileTitle(file);
  const parts = raw
    .split(ENTRY_DELIMITER)
    .map(e => e.trim())
    .filter(e => e.length > 0);

  // The file's first line is an H1 title that matches this file's name
  // ("# MEMORY" / "# USER") and acts as a file header, not a memory entry.
  // Split it off so it never appears as an editable entry in the UI.
  if (parts.length > 0 && parts[0] === header) {
    parts.shift();
  }

  return parts;
}

/**
 * Join entries back into a raw file using the on-disk delimiter.
 * @param entries
 */
function joinEntries(entries: string[]): string {
  return entries.filter(e => e.trim().length > 0).join(ENTRY_DELIMITER);
}

function entries(file: string): string[] {
  return editEntries.value[file] ?? [];
}

/**
 * Length of the joined raw file for one tab (what the server actually measures).
 * @param file
 */
function joinedLength(file: string): number {
  return joinEntries(entries(file)).length;
}

function entryLength(file: string, idx: number): number {
  return (entries(file)[idx] ?? '').length;
}

function addEntry(file: string) {
  editEntries.value[file]?.push('');
}

function removeEntry(file: string, idx: number) {
  editEntries.value[file]?.splice(idx, 1);
}

function setEntry(file: string, idx: number, value: string) {
  const list = editEntries.value[file];
  if (list) list[idx] = value;
}

const loadContent = async () => {
  loading.value = true;
  try {
    const data = await readMemory();
    const parsed: Record<string, string[]> = {};
    for (const tab of tabs) {
      parsed[tab.file] = splitEntries(tab.file, data[tab.file] ?? '');
    }
    editEntries.value = parsed;
    originalEntries.value = {
      ...Object.fromEntries(Object.entries(parsed).map(([k, v]) => [k, [...v]]))
    };
  } catch (e) {
    logUtil.e('[MemoryPanel] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

const canSave = computed(() => {
  if (loading.value || saving.value) return false;
  const tab = tabs[activeTab.value];
  if (!tab) return false;
  const len = joinedLength(tab.file);
  return len > 0 && len <= MAX_CHARS && isDirty(tab.file);
});

function fileChanged(file: string): boolean {
  return joinEntries(entries(file)) !== joinEntries(originalEntries.value[file] ?? []);
}

function isDirty(file: string): boolean {
  const a = editEntries.value[file] ?? [];
  const b = originalEntries.value[file] ?? [];
  if (a.length !== b.length) return true;
  return a.some((e, i) => e !== b[i]);
}

const handleSave = async () => {
  saving.value = true;
  try {
    // Persist only the changed memory files, as full-file content (header + entries).
    const changed: Record<string, string> = {};
    for (const tab of tabs) {
      if (fileChanged(tab.file)) {
        changed[tab.file] = joinFileBody(tab.file, entries(tab.file));
      }
    }
    if (Object.keys(changed).length > 0) {
      await writeMemory(changed);
    }
    emits('saved');
    // The panel stays mounted after a save (it used to close), so the saved
    // entries become the dirty baseline for the next one.
    originalEntries.value = {
      ...Object.fromEntries(Object.entries(editEntries.value).map(([k, v]) => [k, [...v]]))
    };
  } catch (e) {
    logUtil.e('[MemoryPanel] Failed to save:', e);
  } finally {
    saving.value = false;
  }
};

// The tab's lifetime drives the load.
onMounted(loadContent);
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "agent": {
        "tabs": {
          "tools": "工具",
          "middlewares": "中间件",
          "subagents": "代理模型",
          "skills": "技能"
        },
        "models": {
          "scope_main": "主代理",
          "scope_subagent": "子代理",
          "mainHint": "主代理（main agent）使用的模型；默认跟随环境配置。",
          "followEnv": "跟随环境配置"
        },
        "tools": {
          "count": "已选 {selected}/{total}",
          "requiredHint": "必需，不可取消",
          "scope_builtin": "内置",
          "scope_mcp": "MCP",
          "mcpEmpty": "暂无 MCP 工具（在 扩展-MCP 中添加服务器）",
          "clearAll": "全部禁用",
          "clearGroup": "清空",
          "hint": "勾选 main agent 可用的工具（全选 = 默认，不写配置）。被关掉的工具不会出现在模型的工具列表里，旧调用也会被拒绝执行。",
          "selectAll": "全部启用",
          "selectGroup": "全选"
        },
        "skills": {
          "count": "已选 {selected}/{total}",
          "scope_builtin": "内置",
          "scope_thirdparty": "第三方",
          "empty": "暂无技能",
          "hint": "勾选进入系统提示词技能索引的条目（全选 = 默认，不写配置）。取消的条目不会出现在技能索引里。"
        },
        "toolGroup": {
          "files": "文件读写",
          "terminal": "终端与代码",
          "tasks": "任务与计划",
          "memory": "记忆与检索",
          "skills": "技能",
          "subagents": "子代理",
          "interaction": "交互",
          "web": "网络",
          "browser": "浏览器",
          "mcp": "MCP"
        },
        "middlewares": {
          "lockedTitle": "系统必需（锁定）",
          "nudgeLabel": "启用 nudge",
          "nudgeDesc": "压缩后让模型自检是否保存记忆（使用 memory 工具）",
          "hint": "关掉可选项会立即改变该会话的行为；带锁的为系统必需项，不可关闭。",
          "lockedHint": "安全兜底或逻辑必需，关掉会让系统失去保护，故不可关闭",
          "nudgeNeedMemory": "需先在「工具」页勾选 memory 工具"
        },
        "middleware": {
          "TodoContinuationEnforcer": {
            "name": "计划续跑",
            "desc": "计划（待办/任务流）未完成时自动继续下一轮"
          },
          "TaskIntentMiddleware": {
            "name": "任务意图引导",
            "desc": "识别工作型请求并注入编排提示"
          },
          "WorkspaceNoticeMiddleware": {
            "name": "工作区变更通知",
            "desc": "工作目录或 git 分支/HEAD 变化时在用户消息前插入一条通知"
          },
          "MultimodalProcessor": {
            "name": "多模态预处理",
            "desc": "上传的图片/音频/视频入库与降级处理"
          },
          "SubagentCompletionDrainMiddleware": {
            "name": "子代理完成注入",
            "desc": "子代理完成后把结果注入主会话"
          },
          "Summarization": {
            "name": "摘要压缩",
            "desc": "上下文压缩、溢出处理与压缩后的 nudge"
          }
        },
        "subagents": {
          "followTier": "跟随角色默认",
          "tier": "角色默认：{tier}",
          "tierDepth": "无角色定义：按深度默认",
          "hint": "为每类子代理指定使用的模型（来自环境配置的模型档案）；跟随角色默认 = 按角色定义里的 model_tier。",
          "sessionHint": "修改后于 main agent 的下一轮生效（与切换主模型相同）"
        },
        "role": {
          "general": "通用",
          "researcher": "研究员",
          "executor": "执行者",
          "reviewer": "审查者",
          "librarian": "图书管理员"
        },
        "viewer": {
          "tools": "工具 {selected}/{total}",
          "middlewares": "关闭的中间件：{names}",
          "middlewaresNone": "中间件：全部开启",
          "models": "子代理模型：{names}"
        },
        "readonlyHint": "只读：如需修改请到「菜单-预设角色」"
      },
      "save": "保存",
      "cancel": "取消",
      "language": {
        "zh": "简体中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어"
      },
      "tabs": {
        "role": "角色配置",
        "agents": "运行守则",
        "soul": "人格灵魂",
        "user": "用户信息",
        "env": "环境配置",
        "sherry": "应用配置",
        "memoryAgent": "记忆·Agent",
        "memoryUser": "记忆·用户"
      },
      "role": {
        "aiLine": "你将扮演{name}。",
        "userLine": "用户将扮演{name}。",
        "assistant": "AI 角色",
        "userRole": "用户角色",
        "charNote": "名字与头像修改仅在新建会话后生效，旧会话不受影响；角色名字同时写入系统提示词。",
        "noFileChosen": "可选择新的头像图片",
        "resetAvatar": "重置头像",
        "aiName": "AI 名称",
        "userName": "用户名称"
      },
      "persona": {
        "preset": {
          "builtinPureName": "纯净",
          "builtinCompanionName": "情感陪伴",
          "defaultName": "全量",
          "defaultBadge": "默认",
          "builtinCodingName": "编程助手",
          "builtinBadge": "内置",
          "savePreset": "保存预设",
          "apply": "应用",
          "editingBadge": "编辑中",
          "nameDialog": {
            "title": "保存为预设",
            "placeholder": "输入预设名称",
            "confirm": "保存"
          },
          "nameError": {
            "duplicate": "名称已存在",
            "required": "名称不能为空"
          },
          "confirmDelete": {
            "title": "删除预设",
            "message": "确定删除预设「{name}」吗？此操作不可恢复"
          },
          "toast": {
            "presetSaved": "预设已保存",
            "presetDeleted": "预设已删除",
            "applyFailed": "应用失败，请检查后端连接",
            "presetSaveFailed": "预设保存失败",
            "applySuccess": "应用成功"
          },
          "emptyList": "暂无预设",
          "title": "预设角色"
        },
        "restoreDefault": "恢复默认"
      },
      "crop": {
        "confirm": "确定",
        "preview": "裁剪预览",
        "rotate": "旋转",
        "zoomIn": "放大",
        "zoomOut": "缩小",
        "title": "裁剪头像"
      },
      "background": {
        "title": "背景图片",
        "upload": "上传背景",
        "clear": "清除背景",
        "bothThemes": "背景图片在浅色/深色主题下均会显示；通过下方滑块调整遮罩强度。",
        "opacity": "遮罩强度",
        "opacityHint": "浅色主题叠加白色遮罩、深色主题叠加黑色遮罩：越靠右照片越被冲淡成纯白/纯黑，直至完全遮蔽。",
        "cropTitle": "裁剪背景",
        "current": "已设置背景图",
        "noFileChosen": "可选择新图片文件"
      },
      "env": {
        "loadError": "环境配置加载失败，请检查后端服务是否已启动。",
        "saveFailed": "环境配置保存失败，请检查 key 与值是否合法。",
        "restartHint": "修改 API Key 等敏感配置后，需重启后端服务才能生效。",
        "noEnvFile": "未找到 .env 文件。",
        "maxTokenError": "{key} 必须 >= 131072 (128K)，当前值不满足要求，无法保存。",
        "maxTokenHint": "必须 >= 131072 (128K)，否则 Agent 将拒绝启动。"
      },
      "sherry": {
        "loadError": "应用配置加载失败，请检查后端服务是否已启动。",
        "saveFailed": "应用配置保存失败，请检查值是否合法。",
        "restartHint": "修改后需重启后端服务才能生效；配置持久化于项目根目录 sherry.jsonc。",
        "noConfigFile": "未找到 sherry.jsonc 文件。"
      },
      "cron": {
        "addJob": "新建任务",
        "empty": "暂无定时任务。点击「新建任务」添加。",
        "run": "运行",
        "edit": "编辑",
        "delete": "删除",
        "addTitle": "新建定时任务",
        "editTitle": "编辑定时任务",
        "name": "任务名称",
        "namePlaceholder": "例如：每日早安问候",
        "scheduleType": "调度方式",
        "typeAt": "指定时刻",
        "typeEvery": "每隔多久",
        "typeCron": "Cron 表达式",
        "atTime": "执行时刻",
        "everyInterval": "执行间隔",
        "intervalValue": "数值",
        "intervalTooSmall": "间隔最小为 1 秒",
        "cronExpr": "Cron 表达式",
        "message": "任务消息",
        "messagePlaceholder": "请输入要执行的任务内容",
        "skills": "绑定技能（可选）",
        "skillsEmpty": "暂无可用技能",
        "skillsHint": "选中的技能会在执行前预加载 — 定时任务决定何时执行，技能决定如何执行",
        "deliver": "推送到渠道",
        "channel": "渠道名称",
        "channelPlaceholder": "例如：default",
        "to": "接收人/群",
        "toPlaceholder": "可选",
        "deleteAfterRun": "运行后自动删除",
        "nextRun": "下次运行",
        "lastStatus": "上次状态",
        "descAt": "在 {time} 执行",
        "descAtEmpty": "未设定时刻",
        "everySeconds": "每 {n} 秒",
        "everyMinutes": "每 {n} 分钟",
        "everyHours": "每 {n} 小时",
        "everyDays": "每 {n} 天"
      },
      "heartbeat": {
        "addTask": "添加任务",
        "globalSwitch": "全局心跳",
        "globalSwitchHint": "开启中：每 {minutes} 分钟检查一次任务",
        "globalSwitchOff": "已关闭：定时心跳不会触发（重启后仍然关闭）",
        "effectiveHint": "此文件每30分钟被检查一次。请在下方添加希望 Agent 定期处理的任务。若仅剩标题/注释（没有任务），则将跳过心跳。",
        "activeEmpty": "暂无活动任务。请在下方添加新任务。",
        "completedEmpty": "暂无已完成任务。",
        "deleteTask": "删除任务",
        "complete": "完成任务",
        "reactivate": "重新激活"
      },
      "llm": {
        "add": "添加模型",
        "save": "保存",
        "apply": "应用",
        "saved": "已保存",
        "applied": "已应用到 .env",
        "empty": "暂无模型，点击“添加模型”创建",
        "unnamed": "未命名模型",
        "collapse": "收起",
        "count": "{n} / {max}",
        "delete": "删除",
        "deleteConfirm": "确定删除模型“{name}”？",
        "expand": "展开",
        "localModel": "本地模型",
        "localUnused": "本地模式不使用",
        "maxModels": "每个分组最多 {max} 个模型",
        "requiredMissing": "提供商与模型 API 名为必填项",
        "test": "测试连通性",
        "testHint": "用当前填写的参数试调一次，不写入 .env",
        "testUnsupported": "该分组需要图片 / 音频输入，暂不支持连通性测试",
        "testOk": "连通正常（{ms} ms）",
        "testFailed": "连通失败（{ms} ms）",
        "testRequestFailed": "请求失败：后端未响应"
      },
      "memory": {
        "addEntry": "添加条目",
        "deleteEntry": "删除条目",
        "empty": "暂无条目，请在下方添加。",
        "effectiveHint": "提示：修改后仅在系统压缩或重启时生效。"
      },
      "desc": {
        "memoryAgent": "Agent长期记忆笔记（workspace/memory/MEMORY.md）",
        "memoryUser": "用户长期记忆信息（workspace/memory/USER.md）",
        "role": "AI 与用户各自的扮演角色（名字与头像）",
        "agents": "Agent 的运行守则与安全边界",
        "soul": "Agent人格、语气、性格",
        "user": "用户信息和偏好"
      },
      "uploadAvatar": "上传头像"
    }
  },
  "en": {
    "config": {
      "agent": {
        "tabs": {
          "tools": "Tools",
          "middlewares": "Middlewares",
          "subagents": "Agent models",
          "skills": "Skills"
        },
        "models": {
          "scope_main": "Main agent",
          "scope_subagent": "Subagent",
          "mainHint": "The model the main agent runs on; follows the environment config by default.",
          "followEnv": "Follow the environment config"
        },
        "tools": {
          "count": "{selected}/{total} selected",
          "requiredHint": "Required — cannot be disabled",
          "scope_builtin": "Built-in",
          "scope_mcp": "MCP",
          "mcpEmpty": "No MCP tools yet (add a server under Extend → MCP)",
          "clearAll": "Disable all",
          "clearGroup": "Clear",
          "hint": "Pick the tools the main agent may use (all checked = the default; no config is stored). A disabled tool is not offered to the model, and a stale call to it is refused.",
          "selectAll": "Enable all",
          "selectGroup": "All"
        },
        "skills": {
          "count": "{selected}/{total} selected",
          "scope_builtin": "Built-in",
          "scope_thirdparty": "Third-party",
          "empty": "No skills yet",
          "hint": "Check the skills whose index entries enter the system prompt (all checked = default, nothing stored). Unchecked ones never reach the skill index."
        },
        "toolGroup": {
          "files": "Files",
          "terminal": "Terminal & code",
          "tasks": "Tasks & planning",
          "memory": "Memory & search",
          "skills": "Skills",
          "subagents": "Subagents",
          "interaction": "Interaction",
          "web": "Web",
          "browser": "Browser",
          "mcp": "MCP"
        },
        "middlewares": {
          "lockedTitle": "System-required (locked)",
          "nudgeLabel": "Enable nudge",
          "nudgeDesc": "After a compression, asks the model to review what is worth saving (the memory tool)",
          "hint": "Turning an optional entry off changes this session immediately; the locked ones are system-required.",
          "lockedHint": "A safety net or a logical necessity — disabling it would strip protection",
          "nudgeNeedMemory": "Check the memory tool on the Tools tab first"
        },
        "middleware": {
          "TodoContinuationEnforcer": {
            "name": "Plan continuation",
            "desc": "Keeps the turn going while the plan (todos / flows) is unfinished"
          },
          "TaskIntentMiddleware": {
            "name": "Task-intent steering",
            "desc": "Detects work requests and injects the orchestration prompt"
          },
          "WorkspaceNoticeMiddleware": {
            "name": "Workspace-change notice",
            "desc": "Inserts a notice before the user message when the working directory or the git branch/HEAD moved"
          },
          "MultimodalProcessor": {
            "name": "Multimodal preprocessing",
            "desc": "Persists and prepares uploaded images / audio / video"
          },
          "SubagentCompletionDrainMiddleware": {
            "name": "Completion injection",
            "desc": "Injects a finished subagent's result into the main session"
          },
          "Summarization": {
            "name": "Summarization",
            "desc": "Context compression, overflow handling and the post-compression nudges"
          }
        },
        "subagents": {
          "followTier": "Follow the role default",
          "tier": "Role default: {tier}",
          "tierDepth": "No role definition: depth default",
          "hint": "Choose the model each functional role runs on (the environment-config profiles); \"follow the role default\" keeps its model_tier.",
          "sessionHint": "Takes effect on the main agent's next turn (same as switching the main model)"
        },
        "role": {
          "general": "General",
          "researcher": "Researcher",
          "executor": "Executor",
          "reviewer": "Reviewer",
          "librarian": "Librarian"
        },
        "viewer": {
          "tools": "Tools {selected}/{total}",
          "middlewares": "Disabled middlewares: {names}",
          "middlewaresNone": "Middlewares: all on",
          "models": "Subagent models: {names}"
        },
        "readonlyHint": "Read-only — change it in 菜单-预设角色 (Presets & Role)"
      },
      "save": "Save",
      "cancel": "Cancel",
      "language": {
        "zh": "简体中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어"
      },
      "tabs": {
        "role": "Character Setup",
        "agents": "Operating Instructions",
        "soul": "Soul",
        "user": "User Profile",
        "env": "Environment",
        "sherry": "App Config",
        "memoryAgent": "Memory · Agent",
        "memoryUser": "Memory · User"
      },
      "role": {
        "aiLine": "You will play {name}.",
        "userLine": "The user will play {name}.",
        "assistant": "AI Role",
        "userRole": "User Role",
        "charNote": "Name and avatar changes only take effect in new sessions; existing sessions are not affected. The role names are also written into the system prompt.",
        "noFileChosen": "Select an avatar image file",
        "resetAvatar": "Reset avatar",
        "aiName": "AI Name",
        "userName": "User Name"
      },
      "persona": {
        "preset": {
          "builtinPureName": "Pure",
          "builtinCompanionName": "Companion",
          "defaultName": "Full",
          "defaultBadge": "Default",
          "builtinCodingName": "Coding Assistant",
          "builtinBadge": "Built-in",
          "savePreset": "Save Preset",
          "apply": "Apply",
          "editingBadge": "Editing",
          "nameDialog": {
            "title": "Save as Preset",
            "placeholder": "Enter preset name",
            "confirm": "Save"
          },
          "nameError": {
            "duplicate": "Name already exists",
            "required": "Name is required"
          },
          "confirmDelete": {
            "title": "Delete Preset",
            "message": "Delete preset \"{name}\"? This cannot be undone"
          },
          "toast": {
            "presetSaved": "Preset saved",
            "presetDeleted": "Preset deleted",
            "applyFailed": "Apply failed, check backend connection",
            "presetSaveFailed": "Failed to save preset",
            "applySuccess": "Applied successfully"
          },
          "emptyList": "No presets yet",
          "title": "Presets & Role"
        },
        "restoreDefault": "Restore Default"
      },
      "crop": {
        "confirm": "Confirm",
        "preview": "Crop Preview",
        "rotate": "Rotate",
        "zoomIn": "Zoom In",
        "zoomOut": "Zoom Out",
        "title": "Crop Avatar"
      },
      "background": {
        "title": "Background Image",
        "upload": "Upload Background",
        "clear": "Clear Background",
        "bothThemes": "The background image shows in both light and dark themes; adjust the overlay strength with the slider below.",
        "opacity": "Overlay Strength",
        "opacityHint": "A white overlay is used in light theme, black in dark theme: the further right, the more the photo fades to solid white/black until fully covered.",
        "cropTitle": "Crop Background",
        "current": "Background image set",
        "noFileChosen": "Select an image file"
      },
      "env": {
        "loadError": "Failed to load environment config. Please check the backend service.",
        "saveFailed": "Failed to save environment config.",
        "restartHint": "After changing sensitive values (e.g. API keys), restart the backend service for the changes to take effect.",
        "noEnvFile": "No .env file found.",
        "maxTokenError": "{key} must be >= 131072 (128K); current value does not meet the requirement, cannot save.",
        "maxTokenHint": "Must be >= 131072 (128K), otherwise the agent will refuse to start."
      },
      "sherry": {
        "loadError": "Failed to load app config. Please check the backend service.",
        "saveFailed": "Failed to save app config.",
        "restartHint": "Restart the backend service for changes to take effect. Persisted in sherry.jsonc at the project root.",
        "noConfigFile": "No sherry.jsonc file found."
      },
      "cron": {
        "addJob": "New task",
        "empty": "No cron tasks yet. Click \"New task\" to add one.",
        "run": "Run",
        "edit": "Edit",
        "delete": "Delete",
        "addTitle": "New Cron Task",
        "editTitle": "Edit Cron Task",
        "name": "Task name",
        "namePlaceholder": "e.g. Daily good-morning greeting",
        "scheduleType": "Schedule type",
        "typeAt": "Specific time",
        "typeEvery": "Repeat every",
        "typeCron": "Cron expression",
        "atTime": "Execution time",
        "everyInterval": "Interval",
        "intervalValue": "Value",
        "intervalTooSmall": "Interval must be at least 1 second",
        "cronExpr": "Cron expression",
        "message": "Task message",
        "messagePlaceholder": "Enter the task content to execute",
        "skills": "Skills (optional)",
        "skillsEmpty": "No skills installed",
        "skillsHint": "Selected skills are loaded before the prompt runs — the cron sets when, the skill sets how",
        "deliver": "Push to channel",
        "channel": "Channel",
        "channelPlaceholder": "e.g. default",
        "to": "Recipient / group",
        "toPlaceholder": "Optional",
        "deleteAfterRun": "Delete after run",
        "nextRun": "Next run",
        "lastStatus": "Last status",
        "descAt": "Run at {time}",
        "descAtEmpty": "No time set",
        "everySeconds": "Every {n} seconds",
        "everyMinutes": "Every {n} minutes",
        "everyHours": "Every {n} hours",
        "everyDays": "Every {n} days"
      },
      "heartbeat": {
        "addTask": "Add task",
        "globalSwitch": "Global heartbeat",
        "globalSwitchHint": "On: checks the tasks every {minutes} minutes",
        "globalSwitchOff": "Off: the periodic heartbeat does not fire (and stays off after a restart)",
        "effectiveHint": "This file is checked every 30 minutes. Add tasks below for the agent to work on periodically. If no tasks remain (only the headers and comments), the heartbeat is skipped.",
        "activeEmpty": "No active tasks. Add new tasks below.",
        "completedEmpty": "No completed tasks yet.",
        "deleteTask": "Delete task",
        "complete": "Complete",
        "reactivate": "Reactivate"
      },
      "llm": {
        "add": "Add model",
        "save": "Save",
        "apply": "Apply",
        "saved": "Saved",
        "applied": "Applied to .env",
        "empty": "No models yet — click “Add model”",
        "unnamed": "Unnamed model",
        "collapse": "Collapse",
        "count": "{n} / {max}",
        "delete": "Delete",
        "deleteConfirm": "Delete model “{name}”?",
        "expand": "Expand",
        "localModel": "Local model",
        "localUnused": "Unused in local mode",
        "maxModels": "Up to {max} models per group",
        "requiredMissing": "Provider and model API name are required",
        "test": "Test connection",
        "testHint": "Probe once with the parameters on screen — nothing is written to .env",
        "testUnsupported": "This group needs an image / audio payload, so it cannot be probed",
        "testOk": "Reachable ({ms} ms)",
        "testFailed": "Unreachable ({ms} ms)",
        "testRequestFailed": "request failed: the backend did not answer"
      },
      "memory": {
        "addEntry": "Add entry",
        "deleteEntry": "Delete entry",
        "empty": "No entries yet. Add one below.",
        "effectiveHint": "Note: Changes only take effect after compression or restart."
      },
      "desc": {
        "memoryAgent": "Agent long-term memory notes (workspace/memory/MEMORY.md)",
        "memoryUser": "User long-term memory info (workspace/memory/USER.md)",
        "role": "Who the AI and the user each play (names and avatars)",
        "agents": "The agent's operating instructions and safety boundaries",
        "soul": "Agent personality, tone, character",
        "user": "User info and preferences"
      },
      "uploadAvatar": "Upload Avatar"
    }
  },
  "ja": {
    "config": {
      "agent": {
        "tabs": {
          "tools": "ツール",
          "middlewares": "ミドルウェア",
          "subagents": "エージェントモデル",
          "skills": "スキル"
        },
        "models": {
          "scope_main": "メインエージェント",
          "scope_subagent": "サブエージェント",
          "mainHint": "メインエージェントが使うモデル。既定では環境設定に従います。",
          "followEnv": "環境設定に従う"
        },
        "tools": {
          "count": "{selected}/{total} 選択",
          "requiredHint": "必須（解除できません）",
          "scope_builtin": "内蔵",
          "scope_mcp": "MCP",
          "mcpEmpty": "MCP ツールはまだありません（拡張-MCP でサーバーを追加）",
          "clearAll": "すべて無効",
          "clearGroup": "クリア",
          "hint": "main agent が使えるツールを選択（すべて選択 = 既定、設定は保存されません）。無効なツールはモデルに提示されず、古い呼び出しも拒否されます。",
          "selectAll": "すべて有効",
          "selectGroup": "全選択"
        },
        "skills": {
          "count": "{selected}/{total} 選択",
          "scope_builtin": "内蔵",
          "scope_thirdparty": "サードパーティ",
          "empty": "スキルはまだありません",
          "hint": "システムプロンプトのスキル索引に入れる項目を選択します（すべて選択 = 既定、設定は保存されません）。外した項目はスキル索引に現れません。"
        },
        "toolGroup": {
          "files": "ファイル",
          "terminal": "ターミナルとコード",
          "tasks": "タスクと計画",
          "memory": "記憶と検索",
          "skills": "スキル",
          "subagents": "サブエージェント",
          "interaction": "対話",
          "web": "ウェブ",
          "browser": "ブラウザ",
          "mcp": "MCP"
        },
        "middlewares": {
          "lockedTitle": "システム必須（ロック）",
          "nudgeLabel": "nudge を有効化",
          "nudgeDesc": "圧縮後にモデルへ記憶の保存可否を自己点検させます（memory ツール）",
          "hint": "任意項目をオフにするとこのセッションの挙動が変わります。ロック付きはシステム必須です。",
          "lockedHint": "安全網または論理上の必需 — 無効にすると保護が失われます",
          "nudgeNeedMemory": "先に「ツール」タブで memory ツールを選択してください"
        },
        "middleware": {
          "TodoContinuationEnforcer": {
            "name": "計画の継続",
            "desc": "計画（todo / フロー）が未完了の間ターンを継続します"
          },
          "TaskIntentMiddleware": {
            "name": "タスク意図の誘導",
            "desc": "作業依頼を検出してオーケストレーション プロンプトを注入します"
          },
          "WorkspaceNoticeMiddleware": {
            "name": "ワークスペース変更通知",
            "desc": "作業ディレクトリまたは git ブランチ/HEAD が変わったときユーザー メッセージの前に通知を挿入します"
          },
          "MultimodalProcessor": {
            "name": "マルチモーダル前処理",
            "desc": "アップロードされた画像 / 音声 / 動画を保存・前処理します"
          },
          "SubagentCompletionDrainMiddleware": {
            "name": "完了結果の注入",
            "desc": "サブエージェントの完了結果をメイン セッションに注入します"
          },
          "Summarization": {
            "name": "要約圧縮",
            "desc": "コンテキスト圧縮・オーバーフロー処理・圧縮後の nudge"
          }
        },
        "subagents": {
          "followTier": "ロール既定に従う",
          "tier": "ロール既定: {tier}",
          "tierDepth": "ロール定義なし: 深度の既定",
          "hint": "機能ロールごとに使うモデル（環境構成のモデル プロファイル）を指定します。「ロール既定に従う」は model_tier のままです。",
          "sessionHint": "変更は main agent の次のターンで反映されます（メインモデルの切替と同じ）"
        },
        "role": {
          "general": "汎用",
          "researcher": "リサーチャー",
          "executor": "実行者",
          "reviewer": "レビュアー",
          "librarian": "ライブラリアン"
        },
        "viewer": {
          "tools": "ツール {selected}/{total}",
          "middlewares": "無効なミドルウェア: {names}",
          "middlewaresNone": "ミドルウェア: すべて有効",
          "models": "サブエージェント モデル: {names}"
        },
        "readonlyHint": "読み取り専用 — 変更は「メニュー-プリセットと役割」で行います"
      },
      "save": "保存",
      "cancel": "キャンセル",
      "language": {
        "zh": "简体中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어"
      },
      "tabs": {
        "role": "キャラクター設定",
        "agents": "操作指示",
        "soul": "人格・魂",
        "user": "ユーザー情報",
        "env": "環境設定",
        "sherry": "アプリ設定",
        "memoryAgent": "記憶・Agent",
        "memoryUser": "記憶・ユーザー"
      },
      "role": {
        "aiLine": "あなたは{name}を演じます。",
        "userLine": "ユーザーは{name}を演じます。",
        "assistant": "AI ロール",
        "userRole": "ユーザーロール",
        "charNote": "名前とアバターの変更は新しいセッション作成後にのみ反映され、既存のセッションには影響しません。役割名はシステムプロンプトにも書き込まれます。",
        "noFileChosen": "新しいアバター画像を選択できます",
        "resetAvatar": "アバターをリセット",
        "aiName": "AI 名前",
        "userName": "ユーザー名"
      },
      "persona": {
        "preset": {
          "builtinPureName": "純粋",
          "builtinCompanionName": "寄り添い",
          "defaultName": "フル",
          "defaultBadge": "デフォルト",
          "builtinCodingName": "コーディングアシスタント",
          "builtinBadge": "内蔵",
          "savePreset": "プリセット保存",
          "apply": "適用",
          "editingBadge": "編集中",
          "nameDialog": {
            "title": "プリセットとして保存",
            "placeholder": "プリセット名を入力",
            "confirm": "保存"
          },
          "nameError": {
            "duplicate": "名前は既に存在します",
            "required": "名前は必須です"
          },
          "confirmDelete": {
            "title": "プリセットを削除",
            "message": "プリセット「{name}」を削除しますか？元に戻せません"
          },
          "toast": {
            "presetSaved": "プリセットを保存しました",
            "presetDeleted": "プリセットを削除しました",
            "applyFailed": "適用に失敗しました。バックエンド接続を確認してください",
            "presetSaveFailed": "プリセットの保存に失敗しました",
            "applySuccess": "適用しました"
          },
          "emptyList": "プリセットなし",
          "title": "プリセットと役割"
        },
        "restoreDefault": "デフォルトに戻す"
      },
      "crop": {
        "confirm": "確定",
        "preview": "トリミングプレビュー",
        "rotate": "回転",
        "zoomIn": "拡大",
        "zoomOut": "縮小",
        "title": "アバターをトリミング"
      },
      "background": {
        "title": "背景画像",
        "upload": "背景をアップロード",
        "clear": "背景をクリア",
        "bothThemes": "背景画像はライト/ダークテーマの両方で表示されます。下のスライダーでオーバーレイの強さを調整します。",
        "opacity": "オーバーレイの強さ",
        "opacityHint": "ライトテーマでは白、ダークテーマでは黒のオーバーレイを重ねます。右に行くほど写真が真っ白/真っ黒に薄れ、完全に覆われます。",
        "cropTitle": "背景をトリミング",
        "current": "背景画像が設定されています",
        "noFileChosen": "画像ファイルを選択"
      },
      "env": {
        "loadError": "環境設定の読み込みに失敗しました。バックエンドサービスを確認してください。",
        "saveFailed": "環境設定の保存に失敗しました。",
        "restartHint": "APIキーなどの機密設定を変更した場合、反映にはバックエンドの再起動が必要です。",
        "noEnvFile": ".env ファイルが見つかりません。",
        "maxTokenError": "{key} は 131072 (128K) 以上である必要があります。現在の値は要件を満たしていません。保存できません。",
        "maxTokenHint": "131072 (128K) 以上である必要があります。そうでない場合、エージェントは起動を拒否します。"
      },
      "sherry": {
        "loadError": "アプリ設定の読み込みに失敗しました。バックエンドサービスを確認してください。",
        "saveFailed": "アプリ設定の保存に失敗しました。",
        "restartHint": "変更を反映するにはバックエンドの再起動が必要です。設定はプロジェクトルートの sherry.jsonc に保存されます。",
        "noConfigFile": "sherry.jsonc ファイルが見つかりません。"
      },
      "cron": {
        "addJob": "新規タスク",
        "empty": "クーロンタスクはまだありません。「新規タスク」をクリックして追加してください。",
        "run": "実行",
        "edit": "編集",
        "delete": "削除",
        "addTitle": "新しいクーロンタスク",
        "editTitle": "クーロンタスクを編集",
        "name": "タスク名",
        "namePlaceholder": "例：毎朝の挨拶",
        "scheduleType": "スケジュール方式",
        "typeAt": "指定時刻",
        "typeEvery": "間隔で繰り返し",
        "typeCron": "Cron 式",
        "atTime": "実行時刻",
        "everyInterval": "実行間隔",
        "intervalValue": "値",
        "intervalTooSmall": "間隔は1秒以上にしてください",
        "cronExpr": "Cron 式",
        "message": "タスク内容",
        "messagePlaceholder": "実行するタスクの内容を入力",
        "skills": "スキル（任意）",
        "skillsEmpty": "利用可能なスキルがありません",
        "skillsHint": "選択したスキルは実行前にロードされます — クーロンが実行タイミングを、スキルが実行方法を決定します",
        "deliver": "チャネルへ配信",
        "channel": "チャネル名",
        "channelPlaceholder": "例：default",
        "to": "受信者 / グループ",
        "toPlaceholder": "任意",
        "deleteAfterRun": "実行後に自動削除",
        "nextRun": "次回実行",
        "lastStatus": "前回の状態",
        "descAt": "{time} に実行",
        "descAtEmpty": "時刻が未設定",
        "everySeconds": "{n} 秒ごと",
        "everyMinutes": "{n} 分ごと",
        "everyHours": "{n} 時間ごと",
        "everyDays": "{n} 日ごと"
      },
      "heartbeat": {
        "addTask": "タスクを追加",
        "globalSwitch": "グローバル ハートビート",
        "globalSwitchHint": "{minutes} 分ごとにタスクを確認します",
        "globalSwitchOff": "オフ：定期ハートビートは実行されません（再起動後もオフのまま）",
        "effectiveHint": "このファイルは30分ごとにチェックされます。Agentが定期的に処理してほしいタスクを下に追加してください。見出し/コメントのみ（タスクがない）場合はハートビートをスキップします。",
        "activeEmpty": "アクティブなタスクはありません。下に新しいタスクを追加してください。",
        "completedEmpty": "完了済みのタスクはまだありません。",
        "deleteTask": "タスクを削除",
        "complete": "完了にする",
        "reactivate": "再アクティブ化"
      },
      "llm": {
        "add": "モデルを追加",
        "save": "保存",
        "apply": "適用",
        "saved": "保存しました",
        "applied": ".env に適用済み",
        "empty": "モデルがありません。「モデルを追加」をクリック",
        "unnamed": "名称未設定のモデル",
        "collapse": "折りたたむ",
        "count": "{n} / {max}",
        "delete": "削除",
        "deleteConfirm": "モデル「{name}」を削除しますか？",
        "expand": "展開",
        "localModel": "ローカルモデル",
        "localUnused": "ローカルでは未使用",
        "maxModels": "1グループにつき最大 {max} モデル",
        "requiredMissing": "プロバイダーとモデル API 名は必須です",
        "test": "接続テスト",
        "testHint": "画面のパラメータで 1 回だけ試します（.env には書き込みません）",
        "testUnsupported": "このグループは画像 / 音声入力が必要なため、接続テストはできません",
        "testOk": "接続 OK（{ms} ms）",
        "testFailed": "接続失敗（{ms} ms）",
        "testRequestFailed": "リクエスト失敗: バックエンドが応答しません"
      },
      "memory": {
        "addEntry": "項目を追加",
        "deleteEntry": "項目を削除",
        "empty": "項目はまだありません。下から追加してください。",
        "effectiveHint": "注意：変更は圧縮時または再起動時にのみ反映されます。"
      },
      "desc": {
        "memoryAgent": "Agent 長期記憶ノート（workspace/memory/MEMORY.md）",
        "memoryUser": "ユーザー長期記憶情報（workspace/memory/USER.md）",
        "role": "AI とユーザーがそれぞれ演じる役割（名前とアバター）",
        "agents": "Agent の操作指示と安全境界",
        "soul": "Agent 人格、トーン、性格",
        "user": "ユーザー情報と好み"
      },
      "uploadAvatar": "アバターをアップロード"
    }
  },
  "ko": {
    "config": {
      "agent": {
        "tabs": {
          "tools": "도구",
          "middlewares": "미들웨어",
          "subagents": "에이전트 모델",
          "skills": "스킬"
        },
        "models": {
          "scope_main": "메인 에이전트",
          "scope_subagent": "서브에이전트",
          "mainHint": "메인 에이전트가 사용하는 모델입니다. 기본값은 환경 설정을 따릅니다.",
          "followEnv": "환경 설정 따르기"
        },
        "tools": {
          "count": "{selected}/{total} 선택",
          "requiredHint": "필수 — 해제할 수 없음",
          "scope_builtin": "내장",
          "scope_mcp": "MCP",
          "mcpEmpty": "아직 MCP 도구가 없습니다(확장-MCP에서 서버 추가)",
          "clearAll": "모두 끄기",
          "clearGroup": "지우기",
          "hint": "main agent가 사용할 도구를 선택하세요(모두 선택 = 기본, 설정을 저장하지 않음). 꺼진 도구는 모델에 제공되지 않고 이전 호출도 거부됩니다.",
          "selectAll": "모두 켜기",
          "selectGroup": "전체"
        },
        "skills": {
          "count": "{selected}/{total} 선택",
          "scope_builtin": "내장",
          "scope_thirdparty": "서드파티",
          "empty": "아직 스킬이 없습니다",
          "hint": "시스템 프롬프트의 스킬 색인에 들어갈 항목을 선택하세요(모두 선택 = 기본, 설정 저장 안 함). 해제한 항목은 스킬 색인에 나타나지 않습니다."
        },
        "toolGroup": {
          "files": "파일",
          "terminal": "터미널과 코드",
          "tasks": "작업과 계획",
          "memory": "기억과 검색",
          "skills": "스킬",
          "subagents": "서브에이전트",
          "interaction": "상호작용",
          "web": "웹",
          "browser": "브라우저",
          "mcp": "MCP"
        },
        "middlewares": {
          "lockedTitle": "시스템 필수(잠금)",
          "nudgeLabel": "nudge 사용",
          "nudgeDesc": "압축 후 모델이 저장할 만한 내용을 스스로 점검합니다(memory 도구)",
          "hint": "선택 항목을 끄면 이 세션의 동작이 바로 바뀝니다. 자물쇠 항목은 시스템 필수입니다.",
          "lockedHint": "안전망 또는 논리적 필수 — 끄면 보호가 사라집니다",
          "nudgeNeedMemory": "먼저 「도구」 탭에서 memory 도구를 선택하세요"
        },
        "middleware": {
          "TodoContinuationEnforcer": {
            "name": "계획 이어가기",
            "desc": "계획(todo / 플로우)이 끝나지 않은 동안 턴을 이어갑니다"
          },
          "TaskIntentMiddleware": {
            "name": "작업 의도 유도",
            "desc": "작업 요청을 감지해 오케스트레이션 프롬프트를 주입합니다"
          },
          "WorkspaceNoticeMiddleware": {
            "name": "워크스페이스 변경 알림",
            "desc": "작업 디렉터리나 git 브랜치/HEAD가 바뀌면 사용자 메시지 앞에 알림을 삽입합니다"
          },
          "MultimodalProcessor": {
            "name": "멀티모달 전처리",
            "desc": "업로드된 이미지 / 오디오 / 비디오를 저장하고 전처리합니다"
          },
          "SubagentCompletionDrainMiddleware": {
            "name": "완료 결과 주입",
            "desc": "서브에이전트 완료 결과를 메인 세션에 주입합니다"
          },
          "Summarization": {
            "name": "요약 압축",
            "desc": "컨텍스트 압축, 오버플로 처리, 압축 후 nudge"
          }
        },
        "subagents": {
          "followTier": "역할 기본값 따르기",
          "tier": "역할 기본값: {tier}",
          "tierDepth": "역할 정의 없음: 깊이 기본값",
          "hint": "기능 역할마다 사용할 모델(환경 구성 모델 프로필)을 지정하세요. \"역할 기본값 따르기\"는 model_tier를 유지합니다.",
          "sessionHint": "변경은 main agent의 다음 턴에 적용됩니다(메인 모델 전환과 동일)"
        },
        "role": {
          "general": "범용",
          "researcher": "리서처",
          "executor": "실행자",
          "reviewer": "리뷰어",
          "librarian": "사서"
        },
        "viewer": {
          "tools": "도구 {selected}/{total}",
          "middlewares": "꺼진 미들웨어: {names}",
          "middlewaresNone": "미들웨어: 모두 켜짐",
          "models": "서브에이전트 모델: {names}"
        },
        "readonlyHint": "읽기 전용 — 변경은 「메뉴-프리셋과 역할」에서 하세요"
      },
      "save": "저장",
      "cancel": "취소",
      "language": {
        "zh": "简体中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어"
      },
      "tabs": {
        "role": "캐릭터 설정",
        "agents": "운영 지침",
        "soul": "인격·영혼",
        "user": "사용자 정보",
        "env": "환경 설정",
        "sherry": "앱 설정",
        "memoryAgent": "메모리·Agent",
        "memoryUser": "메모리·사용자"
      },
      "role": {
        "aiLine": "당신은 {name} 역할을 연기합니다.",
        "userLine": "사용자는 {name} 역할을 연기합니다.",
        "assistant": "AI 역할",
        "userRole": "사용자 역할",
        "charNote": "이름과 아바타 변경은 새 세션 생성 후에만 적용되며, 기존 세션에는 영향을 주지 않습니다. 역할 이름은 시스템 프롬프트에도 기록됩니다.",
        "noFileChosen": "새 아바타 이미지를 선택할 수 있습니다",
        "resetAvatar": "아바타 초기화",
        "aiName": "AI 이름",
        "userName": "사용자 이름"
      },
      "persona": {
        "preset": {
          "builtinPureName": "순수",
          "builtinCompanionName": "감정 동반",
          "defaultName": "전체",
          "defaultBadge": "기본",
          "builtinCodingName": "코딩 어시스턴트",
          "builtinBadge": "내장",
          "savePreset": "프리셋 저장",
          "apply": "적용",
          "editingBadge": "편집 중",
          "nameDialog": {
            "title": "프리셋으로 저장",
            "placeholder": "프리셋 이름 입력",
            "confirm": "저장"
          },
          "nameError": {
            "duplicate": "이름이 이미 존재합니다",
            "required": "이름은 필수입니다"
          },
          "confirmDelete": {
            "title": "프리셋 삭제",
            "message": "프리셋 \"{name}\"을(를) 삭제하시겠습니까? 되돌릴 수 없습니다"
          },
          "toast": {
            "presetSaved": "프리셋이 저장되었습니다",
            "presetDeleted": "프리셋이 삭제되었습니다",
            "applyFailed": "적용 실패, 백엔드 연결을 확인하세요",
            "presetSaveFailed": "프리셋 저장 실패",
            "applySuccess": "적용됨"
          },
          "emptyList": "프리셋 없음",
          "title": "프리셋과 역할"
        },
        "restoreDefault": "기본값 복원"
      },
      "crop": {
        "confirm": "확인",
        "preview": "자르기 미리보기",
        "rotate": "회전",
        "zoomIn": "확대",
        "zoomOut": "축소",
        "title": "아바타 자르기"
      },
      "background": {
        "title": "배경 이미지",
        "upload": "배경 업로드",
        "clear": "배경 지우기",
        "bothThemes": "배경 이미지는 라이트/다크 테마 모두에서 표시됩니다. 아래 슬라이더로 오버레이 강도를 조정하세요.",
        "opacity": "오버레이 강도",
        "opacityHint": "라이트 테마는 흰색, 다크 테마는 검은색 오버레이를 덮습니다. 오른쪽으로 갈수록 사진이 순백/순흑으로 바래다 완전히 가려집니다.",
        "cropTitle": "배경 자르기",
        "current": "배경 이미지가 설정됨",
        "noFileChosen": "이미지 파일을 선택하세요"
      },
      "env": {
        "loadError": "환경 설정을 불러오지 못했습니다. 백엔드 서비스를 확인하세요.",
        "saveFailed": "환경 설정을 저장하지 못했습니다.",
        "restartHint": "API 키 등 민감한 설정을 변경한 경우, 적용하려면 백엔드를 재시작해야 합니다.",
        "noEnvFile": ".env 파일을 찾을 수 없습니다.",
        "maxTokenError": "{key}는 131072 (128K) 이상이어야 합니다. 현재 값이 요구사항을 충족하지 않아 저장할 수 없습니다.",
        "maxTokenHint": "131072 (128K) 이상이어야 합니다. 그렇지 않으면 에이전트가 시작을 거부합니다."
      },
      "sherry": {
        "loadError": "앱 설정을 불러오지 못했습니다. 백엔드 서비스를 확인하세요.",
        "saveFailed": "앱 설정을 저장하지 못했습니다.",
        "restartHint": "변경 사항을 적용하려면 백엔드를 재시작해야 합니다. 설정은 프로젝트 루트의 sherry.jsonc에 저장됩니다.",
        "noConfigFile": "sherry.jsonc 파일을 찾을 수 없습니다."
      },
      "cron": {
        "addJob": "새 작업",
        "empty": "크론 작업이 없습니다. \"새 작업\"을 클릭하여 추가하세요.",
        "run": "실행",
        "edit": "편집",
        "delete": "삭제",
        "addTitle": "새 크론 작업",
        "editTitle": "크론 작업 편집",
        "name": "작업 이름",
        "namePlaceholder": "예: 매일 아침 인사",
        "scheduleType": "스케줄 방식",
        "typeAt": "지정 시각",
        "typeEvery": "간격 반복",
        "typeCron": "Cron 표현식",
        "atTime": "실행 시각",
        "everyInterval": "실행 간격",
        "intervalValue": "값",
        "intervalTooSmall": "간격은 1초 이상이어야 합니다",
        "cronExpr": "Cron 표현식",
        "message": "작업 내용",
        "messagePlaceholder": "실행할 작업 내용을 입력하세요",
        "skills": "스킬 (선택)",
        "skillsEmpty": "사용 가능한 스킬이 없습니다",
        "skillsHint": "선택한 스킬은 실행 전에 로드됩니다 — 크론이 실행 시점을, 스킬이 실행 방법을 결정합니다",
        "deliver": "채널로 전송",
        "channel": "채널 이름",
        "channelPlaceholder": "예: default",
        "to": "수신자 / 그룹",
        "toPlaceholder": "선택 사항",
        "deleteAfterRun": "실행 후 자동 삭제",
        "nextRun": "다음 실행",
        "lastStatus": "마지막 상태",
        "descAt": "{time}에 실행",
        "descAtEmpty": "시각 미설정",
        "everySeconds": "매 {n}초",
        "everyMinutes": "매 {n}분",
        "everyHours": "매 {n}시간",
        "everyDays": "매 {n}일"
      },
      "heartbeat": {
        "addTask": "작업 추가",
        "globalSwitch": "전역 하트비트",
        "globalSwitchHint": "{minutes}분마다 작업을 확인합니다",
        "globalSwitchOff": "꺼짐: 주기 하트비트가 실행되지 않습니다(재시작 후에도 꺼진 상태 유지)",
        "effectiveHint": "이 파일은 30분마다 확인됩니다. 에이전트가 주기적으로 처리하길 원하는 작업을 아래에 추가하세요. 헤더/주석만 남고 작업이 없으면 하트비트를 건너뜁니다.",
        "activeEmpty": "활성 작업이 없습니다. 아래에서 새 작업을 추가하세요.",
        "completedEmpty": "완료된 작업이 아직 없습니다.",
        "deleteTask": "작업 삭제",
        "complete": "완료로 이동",
        "reactivate": "다시 활성화"
      },
      "llm": {
        "add": "모델 추가",
        "save": "저장",
        "apply": "적용",
        "saved": "저장됨",
        "applied": ".env에 적용됨",
        "empty": "모델이 없습니다. “모델 추가”를 클릭하세요",
        "unnamed": "이름 없는 모델",
        "collapse": "접기",
        "count": "{n} / {max}",
        "delete": "삭제",
        "deleteConfirm": "모델 “{name}”을(를) 삭제할까요?",
        "expand": "펼치기",
        "localModel": "로컬 모델",
        "localUnused": "로컬 모드에서 미사용",
        "maxModels": "그룹당 최대 {max}개 모델",
        "requiredMissing": "제공자와 모델 API 이름은 필수입니다",
        "test": "연결 테스트",
        "testHint": "화면의 파라미터로 한 번 호출합니다(.env에 쓰지 않습니다)",
        "testUnsupported": "이 그룹은 이미지 / 오디오 입력이 필요해 연결 테스트를 할 수 없습니다",
        "testOk": "연결 정상({ms} ms)",
        "testFailed": "연결 실패({ms} ms)",
        "testRequestFailed": "요청 실패: 백엔드가 응답하지 않음"
      },
      "memory": {
        "addEntry": "항목 추가",
        "deleteEntry": "항목 삭제",
        "empty": "항목이 없습니다. 아래에서 추가하세요.",
        "effectiveHint": "참고: 변경 사항은 압축 또는 재시작 시에만 적용됩니다."
      },
      "desc": {
        "memoryAgent": "Agent 장기 기억 메모 (workspace/memory/MEMORY.md)",
        "memoryUser": "사용자 장기 기억 정보 (workspace/memory/USER.md)",
        "role": "AI와 사용자가 각각 맡는 역할(이름과 아바타)",
        "agents": "Agent의 운영 지침과 안전 경계",
        "soul": "Agent 인격, 어조, 성격",
        "user": "사용자 정보 및 선호도"
      },
      "uploadAvatar": "아바타 업로드"
    }
  }
}
</i18n>

<style scoped>
/* The tab strip keeps its height and the tab body scrolls inside it, so the save
   action below stays pinned at the bottom-right of the panel instead of scrolling
   away with a long form. */
:deep(.p-tabview) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}

/* This PrimeVue version renders the strip as .p-tabview-tablist-container
   (there is no .p-tabview-nav): keep it at its own height so the panels area
   below is the only thing that scrolls. */
:deep(.p-tabview-tablist-container) {
  flex-shrink: 0;
}

:deep(.p-tabview-panels) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}
</style>
