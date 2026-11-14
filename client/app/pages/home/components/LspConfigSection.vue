<template>
  <div class="flex flex-col gap-4 min-w-[320px]" data-test="lsp-config">
    <p v-if="error" class="m-0 text-sm text-red-600 dark:text-red-400" data-test="lsp-error">
      {{ error }}
    </p>

    <p class="m-0 text-xs text-gray-500 dark:text-gray-400">
      {{ t('lspConfig.root') }} <span class="font-mono">{{ root || '—' }}</span>
    </p>

    <!-- Global (builtin + skill) declarations: read-only reference. -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('lspConfig.global') }}
        </span>
        <Button
          :label="t('lspConfig.refresh')"
          icon="pi pi-refresh"
          size="small"
          text
          severity="secondary"
          :loading="loading"
          data-test="lsp-refresh"
          @click="load" />
      </div>
      <small class="text-xs text-gray-400 dark:text-gray-500">{{ t('lspConfig.globalHint') }}</small>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-xs">
          <thead class="text-gray-400 dark:text-gray-500">
            <tr>
              <th class="py-1 pr-2">{{ t('lspConfig.language') }}</th>
              <th class="py-1 pr-2">{{ t('lspConfig.command') }}</th>
              <th class="py-1 pr-2">{{ t('lspConfig.extensions') }}</th>
              <th class="py-1 pr-2">{{ t('lspConfig.source') }}</th>
              <th class="py-1">{{ t('lspConfig.installed') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in globalServers"
              :key="`global-${entry.language}`"
              class="border-t border-gray-100 dark:border-gray-800"
              :data-test="`lsp-global-${entry.language}`">
              <td class="py-1 pr-2 font-mono">{{ entry.language }}</td>
              <td class="py-1 pr-2 font-mono">{{ entry.command.join(' ') }}</td>
              <td class="py-1 pr-2 font-mono">{{ entry.extensions.join(' ') }}</td>
              <td class="py-1 pr-2 text-gray-400">{{ entry.source }}</td>
              <td class="py-1">
                <span :class="entry.installed ? 'text-emerald-500' : 'text-gray-400'">
                  {{ entry.installed ? `● ${t('lspConfig.installed')}` : `○ ${t('lspConfig.notInstalled')}` }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- Project layer: editable / uploadable. -->
    <section class="flex flex-col gap-2 rounded-lg border border-gray-100 dark:border-gray-800 p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-sm font-semibold text-gray-700 dark:text-gray-200">
          {{ t('lspConfig.project') }}
        </span>
        <div class="flex flex-wrap gap-2">
          <Button
            :label="t('lspConfig.add')"
            icon="pi pi-plus"
            size="small"
            text
            data-test="lsp-add"
            @click="addRow" />
          <Button
            :label="t('lspConfig.upload')"
            icon="pi pi-upload"
            size="small"
            text
            data-test="lsp-upload"
            @click="uploadInput?.click()" />
          <input
            ref="uploadInput"
            type="file"
            accept=".json,application/json"
            class="hidden"
            data-test="lsp-upload-input"
            @change="onUpload" />
          <Button
            :label="t('lspConfig.save')"
            icon="pi pi-save"
            size="small"
            :loading="saving"
            data-test="lsp-save"
            @click="save" />
        </div>
      </div>
      <small class="text-xs text-gray-400 dark:text-gray-500">{{ t('lspConfig.projectHint') }}</small>

      <p v-if="!rows.length" class="m-0 text-xs text-gray-400" data-test="lsp-project-empty">
        {{ t('lspConfig.empty') }}
      </p>

      <div
        v-for="(row, index) in rows"
        :key="`row-${index}`"
        class="flex flex-col gap-1 rounded border border-gray-100 p-2 dark:border-gray-800"
        data-test="lsp-project-row">
        <div class="flex flex-wrap items-center gap-2">
          <InputText
            v-model="row.language"
            :placeholder="t('lspConfig.languagePlaceholder')"
            class="w-36 font-mono text-xs"
            data-test="lsp-row-language" />
          <InputText
            v-model="row.commandText"
            :placeholder="t('lspConfig.commandPlaceholder')"
            class="flex-1 font-mono text-xs"
            data-test="lsp-row-command" />
          <Button
            icon="pi pi-bolt"
            size="small"
            text
            severity="secondary"
            :aria-label="t('lspConfig.test')"
            data-test="lsp-row-test"
            @click="runTest(row)" />
          <Button
            icon="pi pi-trash"
            size="small"
            text
            severity="danger"
            :aria-label="t('lspConfig.delete')"
            data-test="lsp-row-delete"
            @click="removeRow(row)" />
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <InputText
            v-model="row.extensionsText"
            :placeholder="t('lspConfig.extensionsPlaceholder')"
            class="w-48 font-mono text-xs"
            data-test="lsp-row-extensions" />
          <InputText
            v-model="row.languageId"
            :placeholder="t('lspConfig.languageIdPlaceholder')"
            class="w-36 font-mono text-xs"
            data-test="lsp-row-language-id" />
          <span
            v-if="resultFor(row.language)"
            class="text-xs"
            :class="resultFor(row.language)?.ok ? 'text-emerald-500' : 'text-amber-500'"
            :data-test="`lsp-result-${index}`">
            {{ resultFor(row.language)?.message }}
          </span>
        </div>
      </div>

      <p
        v-if="errors.length"
        class="m-0 whitespace-pre-wrap text-xs text-amber-600 dark:text-amber-400"
        data-test="lsp-project-errors">
        {{ errors.join('\n') }}
      </p>
    </section>
  </div>
</template>

<script lang="ts" setup>
import { computed, onMounted, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import {
  deleteLspLanguage,
  readLspConfig,
  testLspServer,
  uploadLspProject,
  writeLspProject,
  type LspConfigPayload,
  type LspProjectServer,
  type LspServerEntry,
  type LspTestResult
} from '@/composables/lspConfig';
import { toastError, toastSuccess } from '@/composables/toast';
import { logUtil } from '~/utils/log';

interface ProjectRow {
  language: string;
  commandText: string;
  extensionsText: string;
  languageId: string;
}

const { t } = useI18n({ useScope: 'local' });
const route = useRoute();
const sessionId = computed(() => String(route.params.sid ?? ''));

const loading = ref(false);
const saving = ref(false);
const error = ref('');
const root = ref('');
const globalServers = ref<LspServerEntry[]>([]);
const rows = ref<ProjectRow[]>([]);
const errors = ref<string[]>([]);
const results = ref<Record<string, LspTestResult>>({});
const uploadInput = ref<HTMLInputElement | null>(null);

const resultFor = (language: string): LspTestResult | undefined =>
  language ? results.value[language] : undefined;

function toRows(payload: LspConfigPayload): ProjectRow[] {
  return Object.entries(payload.project?.servers ?? {}).map(([language, spec]) => ({
    language,
    commandText: (spec.command ?? []).join(' '),
    extensionsText: (spec.extensions ?? []).join(' '),
    languageId: spec.language_id ?? language
  }));
}

function apply(payload: LspConfigPayload): void {
  root.value = payload.root;
  // The global table is the read-only reference (builtin + skill); project
  // entries live in the editable table below it.
  globalServers.value = (payload.servers ?? []).filter(entry => entry.source !== 'project');
  rows.value = toRows(payload);
  errors.value = payload.errors ?? [];
}

async function load(): Promise<void> {
  if (!sessionId.value) return;
  loading.value = true;
  error.value = '';
  try {
    apply(await readLspConfig(sessionId.value));
  } catch (cause) {
    logUtil.e('[LspConfigSection] load failed', cause);
    error.value = t('lspConfig.loadFailed');
  } finally {
    loading.value = false;
  }
}

function addRow(): void {
  rows.value.push({ language: '', commandText: '', extensionsText: '', languageId: '' });
}

function splitWords(text: string): string[] {
  return text.trim().split(/\s+/).filter(Boolean);
}

function splitExtensions(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map(item => item.trim())
    .filter(Boolean);
}

function buildPayload(): { servers: Record<string, LspProjectServer>; enable: string[] } {
  const servers: Record<string, LspProjectServer> = {};
  const enable: string[] = [];
  for (const row of rows.value) {
    const language = row.language.trim();
    if (!language) continue;
    servers[language] = {
      command: splitWords(row.commandText),
      extensions: splitExtensions(row.extensionsText),
      language_id: row.languageId.trim() || language
    };
    enable.push(language);
  }
  return { servers, enable };
}

async function save(): Promise<void> {
  if (!sessionId.value) return;
  saving.value = true;
  try {
    const result = await writeLspProject(sessionId.value, buildPayload());
    if (!result.success) {
      toastError(t('lspConfig.saveFailed'), result.message);
      return;
    }
    toastSuccess(t('lspConfig.saved'));
    await load();
  } catch (cause) {
    logUtil.e('[LspConfigSection] save failed', cause);
    toastError(t('lspConfig.saveFailed'));
  } finally {
    saving.value = false;
  }
}

async function removeRow(row: ProjectRow): Promise<void> {
  if (!sessionId.value) return;
  const language = row.language.trim();
  if (!language) {
    rows.value = rows.value.filter(item => item !== row);
    return;
  }
  const result = await deleteLspLanguage(sessionId.value, language);
  if (!result.success) {
    toastError(t('lspConfig.saveFailed'), result.message);
    return;
  }
  toastSuccess(t('lspConfig.deleted'));
  await load();
}

async function runTest(row: ProjectRow): Promise<void> {
  if (!sessionId.value) return;
  const language = row.language.trim();
  if (!language) return;
  try {
    const result = await testLspServer(sessionId.value, language);
    results.value = { ...results.value, [language]: result };
    if (result.ok) toastSuccess(t('lspConfig.testOk'), result.message);
    else toastError(t('lspConfig.testFailed'), result.message);
  } catch (cause) {
    logUtil.e('[LspConfigSection] test failed', cause);
    toastError(t('lspConfig.testFailed'));
  }
}

async function onUpload(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = '';
  if (!file || !sessionId.value) return;
  try {
    const content = await file.text();
    const result = await uploadLspProject(sessionId.value, content);
    if (!result.success) {
      toastError(t('lspConfig.saveFailed'), result.message);
      return;
    }
    toastSuccess(t('lspConfig.uploaded'));
    await load();
  } catch (cause) {
    logUtil.e('[LspConfigSection] upload failed', cause);
    toastError(t('lspConfig.saveFailed'));
  }
}

onMounted(load);
</script>

<i18n lang="json">
{
  "zh": {
    "lspConfig": {
      "root": "项目根目录：",
      "global": "全局配置（内置 + 插件，只读）",
      "globalHint": "内置表与技能 lsp.json 声明的语言。项目级条目会覆盖同名语言（整条替换）。",
      "refresh": "刷新",
      "language": "语言",
      "command": "命令",
      "extensions": "扩展名",
      "source": "来源",
      "installed": "已安装",
      "notInstalled": "未安装",
      "project": "项目级配置（可编辑）",
      "projectHint": "保存到 <项目>/.sherry/lsp.json；命令第一个词必须是 PATH 中可发现的 server 名，参数空格分隔。上传会整体替换项目层。",
      "add": "添加语言",
      "upload": "上传 lsp.json",
      "save": "保存项目配置",
      "empty": "尚无项目级条目。添加语言或上传 lsp.json。",
      "languagePlaceholder": "语言键（如 elixir）",
      "commandPlaceholder": "命令（如 elixir-ls --stdio）",
      "extensionsPlaceholder": "扩展名（如 .ex .exs）",
      "languageIdPlaceholder": "language_id（可选）",
      "test": "测试",
      "delete": "删除",
      "saved": "项目级 LSP 配置已保存",
      "deleted": "已删除项目级条目",
      "uploaded": "lsp.json 已上传并保存",
      "saveFailed": "保存失败",
      "testOk": "server 初始化成功",
      "testFailed": "server 不可用",
      "loadFailed": "加载 LSP 配置失败，请检查后端服务"
    }
  },
  "en": {
    "lspConfig": {
      "root": "Project root:",
      "global": "Global configuration (builtin + skill, read-only)",
      "globalHint": "Languages from the builtin table and skill lsp.json files. A project entry replaces the same language wholesale.",
      "refresh": "Refresh",
      "language": "Language",
      "command": "Command",
      "extensions": "Extensions",
      "source": "Source",
      "installed": "installed",
      "notInstalled": "not installed",
      "project": "Project configuration (editable)",
      "projectHint": "Saved to <project>/.sherry/lsp.json. The first command word must be a server discoverable on PATH; separate arguments with spaces. Uploading replaces the whole project layer.",
      "add": "Add language",
      "upload": "Upload lsp.json",
      "save": "Save project config",
      "empty": "No project-level entries yet. Add a language or upload lsp.json.",
      "languagePlaceholder": "language key (e.g. elixir)",
      "commandPlaceholder": "command (e.g. elixir-ls --stdio)",
      "extensionsPlaceholder": "extensions (e.g. .ex .exs)",
      "languageIdPlaceholder": "language_id (optional)",
      "test": "Test",
      "delete": "Delete",
      "saved": "Project LSP configuration saved",
      "deleted": "Project entry deleted",
      "uploaded": "lsp.json uploaded and saved",
      "saveFailed": "Save failed",
      "testOk": "server initialized",
      "testFailed": "server unavailable",
      "loadFailed": "Failed to load the LSP configuration — check the backend service"
    }
  },
  "ja": {
    "lspConfig": {
      "root": "プロジェクトルート：",
      "global": "グローバル設定（組み込み + スキル、読み取り専用）",
      "globalHint": "組み込みテーブルとスキルの lsp.json が宣言した言語です。プロジェクト項目は同名の言語を丸ごと置き換えます。",
      "refresh": "更新",
      "language": "言語",
      "command": "コマンド",
      "extensions": "拡張子",
      "source": "ソース",
      "installed": "インストール済み",
      "notInstalled": "未インストール",
      "project": "プロジェクト設定（編集可能）",
      "projectHint": "<project>/.sherry/lsp.json に保存されます。コマンドの先頭は PATH で見つかる server 名である必要があり、引数は空白区切りです。アップロードはプロジェクト層全体を置き換えます。",
      "add": "言語を追加",
      "upload": "lsp.json をアップロード",
      "save": "プロジェクト設定を保存",
      "empty": "プロジェクト項目はまだありません。言語を追加するか lsp.json をアップロードしてください。",
      "languagePlaceholder": "言語キー（例: elixir）",
      "commandPlaceholder": "コマンド（例: elixir-ls --stdio）",
      "extensionsPlaceholder": "拡張子（例: .ex .exs）",
      "languageIdPlaceholder": "language_id（任意）",
      "test": "テスト",
      "delete": "削除",
      "saved": "プロジェクト LSP 設定を保存しました",
      "deleted": "プロジェクト項目を削除しました",
      "uploaded": "lsp.json をアップロードして保存しました",
      "saveFailed": "保存に失敗しました",
      "testOk": "server を初期化しました",
      "testFailed": "server を利用できません",
      "loadFailed": "LSP 設定の読み込みに失敗しました。バックエンドを確認してください"
    }
  },
  "ko": {
    "lspConfig": {
      "root": "프로젝트 루트:",
      "global": "전역 설정 (내장 + 스킬, 읽기 전용)",
      "globalHint": "내장 테이블과 스킬 lsp.json이 선언한 언어입니다. 프로젝트 항목은 같은 언어를 통째로 대체합니다.",
      "refresh": "새로 고침",
      "language": "언어",
      "command": "명령",
      "extensions": "확장자",
      "source": "소스",
      "installed": "설치됨",
      "notInstalled": "미설치",
      "project": "프로젝트 설정 (편집 가능)",
      "projectHint": "<project>/.sherry/lsp.json에 저장됩니다. 명령의 첫 단어는 PATH에서 찾을 수 있는 server 이름이어야 하며 인수는 공백으로 구분합니다. 업로드는 프로젝트 계층 전체를 교체합니다.",
      "add": "언어 추가",
      "upload": "lsp.json 업로드",
      "save": "프로젝트 설정 저장",
      "empty": "프로젝트 항목이 없습니다. 언어를 추가하거나 lsp.json을 업로드하세요.",
      "languagePlaceholder": "언어 키 (예: elixir)",
      "commandPlaceholder": "명령 (예: elixir-ls --stdio)",
      "extensionsPlaceholder": "확장자 (예: .ex .exs)",
      "languageIdPlaceholder": "language_id (선택)",
      "test": "테스트",
      "delete": "삭제",
      "saved": "프로젝트 LSP 설정을 저장했습니다",
      "deleted": "프로젝트 항목을 삭제했습니다",
      "uploaded": "lsp.json을 업로드해 저장했습니다",
      "saveFailed": "저장 실패",
      "testOk": "server 초기화 성공",
      "testFailed": "server 사용 불가",
      "loadFailed": "LSP 설정을 불러오지 못했습니다. 백엔드를 확인하세요"
    }
  }
}
</i18n>
