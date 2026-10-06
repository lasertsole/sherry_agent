<template>
  <!-- Right-sidebar tab body: mounted/unmounted with the tab, which drives the load
       (the sidebar owns the tab label and the close button). The two columns stay
       side by side (the editor scrolls sideways when the sidebar is narrow). -->
  <div class="flex flex-col h-full min-h-0 overflow-y-auto p-4">
    <!-- Side-by-side, always: the editor column keeps its working width as a FLOOR
         and scrolls sideways when the sidebar is narrower, instead of reflowing or
         squeezing its rows. See the column's own note. -->
    <div class="flex min-h-0 flex-1 flex-col gap-3 md:flex-row">
      <!-- Left column: the persona editor + save-preset action. `min-w-0` lets the
           flex item be narrower than its content, `overflow-x-auto` turns that into a
           horizontal scrollbar, and the `min-w-[420px]` floor on the children keeps
           the editor readable (the rows truncate) instead of collapsing into a
           one-character-per-line sliver. -->
      <div
        class="flex min-w-0 min-h-0 flex-1 flex-col gap-3 overflow-x-auto"
        data-test="persona-editor-column">
        <div
          v-if="loading"
          class="flex items-center justify-center py-8">
          <ProgressSpinner style="width: 2rem; height: 2rem" />
        </div>
        <template v-else>
          <TabView
            v-model:activeIndex="activeTab"
            class="flex min-h-0 min-w-[420px] flex-1 flex-col">
            <!-- FIRST tab: the role config (who the AI plays, who the user plays).
                 Part of the preset: 保存预设 stores the names + avatars with the
                 persona files, and 应用 writes the composed ROLE.md so the role
                 statement reaches the system prompt. -->
            <TabPanel
              value="role"
              :header="t('config.tabs.role')">
              <div
                class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto"
                data-test="persona-role-tab">
                <div class="flex items-center justify-between">
                  <span class="text-sm text-gray-500 dark:text-gray-400">{{ t('config.desc.role') }}</span>
                  <Button
                    :label="t('config.persona.restoreDefault')"
                    icon="pi pi-refresh"
                    severity="secondary"
                    text
                    size="small"
                    @click="restoreRoleDefault" />
                </div>
                <p class="m-0 text-xs font-medium text-red-600 dark:text-red-400">{{ t('config.role.charNote') }}</p>

                <!-- AI role -->
                <div class="flex flex-col gap-2">
                  <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                    t('config.role.assistant')
                  }}</span>
                  <div class="flex items-center gap-3">
                    <!-- An empty avatar field means "no avatar of its own": the neutral
                         gray silhouette renders in its place (never a broken image). -->
                    <img
                      :src="charAssistant.avatar || DEFAULT_PLACEHOLDER_AVATAR"
                      alt="assistant avatar"
                      class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                    <div class="flex flex-col gap-2 flex-1">
                      <InputText
                        v-model="charAssistant.name"
                        data-test="persona-role-ai-name"
                        :placeholder="t('config.role.aiName')"
                        class="w-full" />
                      <div class="flex items-center gap-2">
                        <FileUpload
                          mode="basic"
                          :choose-label="t('config.uploadAvatar')"
                          accept="image/*"
                          customUpload
                          :auto="false"
                          @select="onAssistAvatarSelect">
                          <template #filelabel="{ files }">
                            <span class="text-xs text-gray-400">
                              {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                            </span>
                          </template>
                        </FileUpload>
                        <Button
                          :label="t('config.role.resetAvatar')"
                          icon="pi pi-replay"
                          severity="secondary"
                          text
                          size="small"
                          :disabled="!charAssistant.avatar"
                          data-test="persona-role-ai-avatar-reset"
                          @click="resetAvatar('assistant')" />
                      </div>
                    </div>
                  </div>
                </div>

                <Divider />

                <!-- User role -->
                <div class="flex flex-col gap-2">
                  <span class="text-sm font-medium text-gray-600 dark:text-gray-300">{{
                    t('config.role.userRole')
                  }}</span>
                  <div class="flex items-center gap-3">
                    <img
                      :src="charUser.avatar || DEFAULT_PLACEHOLDER_AVATAR"
                      alt="user avatar"
                      class="w-14 h-14 rounded-full object-cover border border-gray-300 dark:border-gray-700" />
                    <div class="flex flex-col gap-2 flex-1">
                      <InputText
                        v-model="charUser.name"
                        data-test="persona-role-user-name"
                        :placeholder="t('config.role.userName')"
                        class="w-full" />
                      <div class="flex items-center gap-2">
                        <FileUpload
                          mode="basic"
                          :choose-label="t('config.uploadAvatar')"
                          accept="image/*"
                          customUpload
                          :auto="false"
                          @select="onUserAvatarSelect">
                          <template #filelabel="{ files }">
                            <span class="text-xs text-gray-400">
                              {{ avatarFileLabel(Array.isArray(files) ? files : []) }}
                            </span>
                          </template>
                        </FileUpload>
                        <Button
                          :label="t('config.role.resetAvatar')"
                          icon="pi pi-replay"
                          severity="secondary"
                          text
                          size="small"
                          :disabled="!charUser.avatar"
                          data-test="persona-role-user-avatar-reset"
                          @click="resetAvatar('user')" />
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </TabPanel>
            <TabPanel
              v-for="tab in tabs"
              :key="tab.key"
              :value="tab.key"
              :header="t(tab.i18nKey)">
              <div class="flex min-h-0 flex-1 flex-col gap-2">
                <div class="flex items-center justify-between">
                  <span class="text-sm text-gray-500 dark:text-gray-400">{{ t(tab.i18nDescKey) }}</span>
                  <div class="flex items-center gap-2">
                    <Button
                      :label="t('config.persona.restoreDefault')"
                      icon="pi pi-refresh"
                      severity="secondary"
                      text
                      size="small"
                      :loading="restoring"
                      :disabled="restoring"
                      @click="restoreDefault(tab)" />
                    <span
                      :class="[
                        'text-xs',
                        (editContent[tab.key]?.length ?? 0) > MAX_CHARS
                          ? 'text-red-500'
                          : (editContent[tab.key]?.length ?? 0) > MAX_CHARS * 0.9
                            ? 'text-orange-500'
                            : 'text-gray-400'
                      ]">
                      {{ editContent[tab.key]?.length ?? 0 }} / {{ MAX_CHARS }}
                    </span>
                  </div>
                </div>
                <Textarea
                  v-model="editContent[tab.key]"
                  rows="18"
                  class="w-full min-h-0 flex-1 font-mono text-sm"
                  :maxlength="MAX_CHARS"
                  style="overflow: auto" />
              </div>
            </TabPanel>

            <!-- 工具 tab: which main-agent tools this preset/session enables.
                 The catalogue comes from the backend (GET /agent/catalog) — the
                 client never hardcodes tool names. All checked = the default. -->
            <TabPanel
              value="agentTools"
              :header="t('config.agent.tabs.tools')"
              data-test="persona-agent-tools-tab">
              <div
                class="flex min-h-0 min-w-0 flex-1 flex-col gap-2"
                data-test="agent-tools-tab">
                <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                  <span class="min-w-0 flex-1 text-sm text-gray-500 dark:text-gray-400">
                    {{ t('config.agent.tools.hint') }}
                  </span>
                  <div class="flex shrink-0 items-center gap-2">
                    <span class="text-xs text-gray-400">
                      {{
                        t('config.agent.tools.count', {
                          selected: agentEnabledTools.length,
                          total: agentStore.catalog.tools.length
                        })
                      }}
                    </span>
                    <Button
                      :label="t('config.agent.tools.selectAll')"
                      severity="secondary"
                      text
                      size="small"
                      data-test="agent-tools-all"
                      @click="selectAllTools" />
                    <Button
                      :label="t('config.agent.tools.clearAll')"
                      severity="secondary"
                      text
                      size="small"
                      data-test="agent-tools-none"
                      @click="clearAllTools" />
                  </div>
                </div>
                <!-- Two sub-tabs: the built-in groups and the MCP servers' tools. -->
                <div class="flex items-center gap-1">
                  <button
                    v-for="scope in AGENT_TOOL_SCOPES"
                    :key="scope"
                    type="button"
                    class="rounded-full px-2.5 py-0.5 text-xs transition-colors"
                    :class="
                      toolScope === scope
                        ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
                        : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                    "
                    :data-test="`agent-tools-scope-${scope}`"
                    @click="toolScope = scope">
                    {{ t(`config.agent.tools.scope_${scope}`) }}
                  </button>
                </div>
                <div class="min-h-0 min-w-0 flex-1 overflow-y-auto pr-1">
                  <div
                    v-if="agentVisibleToolGroups.length === 0"
                    class="px-3 py-1 text-xs text-gray-400"
                    data-test="agent-tools-empty">
                    {{ t('config.agent.tools.mcpEmpty') }}
                  </div>
                  <div
                    v-for="group in agentVisibleToolGroups"
                    :key="group.group"
                    class="mb-3 rounded-lg border border-solid border-gray-light p-2 dark:border-[#555]">
                    <div class="mb-1 flex items-center justify-between gap-2">
                      <span class="text-xs font-semibold text-gray-600 dark:text-gray-300">
                        {{ t(`config.agent.toolGroup.${group.group}`) }}
                      </span>
                      <span
                        v-if="agentGroupFullyRequired(group)"
                        class="inline-flex items-center gap-1 text-[11px] text-gray-400"
                        :data-test="`agent-tool-group-locked-${group.group}`">
                        <i class="pi pi-lock text-[9px]" />
                        {{ t('config.agent.tools.requiredHint') }}
                      </span>
                      <Button
                        v-else
                        :label="
                          agentGroupFullySelected(group)
                            ? t('config.agent.tools.clearGroup')
                            : t('config.agent.tools.selectGroup')
                        "
                        severity="secondary"
                        text
                        size="small"
                        :data-test="`agent-tool-group-${group.group}`"
                        @click="toggleToolGroup(group)" />
                    </div>
                    <!-- Two row shapes: a switch (checkbox) for anything the preset may
                         narrow, and a plain chip for what it may not — a REQUIRED tool
                         (no checkbox at all, just the lock) or a bulk-only group's
                         membership (the header owns its only toggle). -->
                    <div class="flex flex-wrap gap-x-4 gap-y-1">
                      <label
                        v-for="tool in group.tools"
                        :key="tool.name"
                        class="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300"
                        :class="agentToolSwitchVisible(group, tool) ? 'cursor-pointer' : 'cursor-default'"
                        :title="toolTooltip(tool)">
                        <Checkbox
                          v-if="agentToolSwitchVisible(group, tool)"
                          :model-value="agentToolSelected(tool.name)"
                          binary
                          :data-test="`agent-tool-${tool.name}`"
                          @update:model-value="toggleTool(tool.name)" />
                        <i
                          v-if="tool.required"
                          class="pi pi-lock text-[9px] text-gray-400" />
                        <span
                          class="font-mono"
                          :class="{
                            'text-gray-400 line-through': !tool.required && !agentToolSelected(tool.name)
                          }"
                          :data-test="agentToolChipTestId(group, tool)">
                          {{ tool.name }}
                        </span>
                      </label>
                    </div>
                  </div>
                </div>
              </div>
            </TabPanel>

            <!-- 中间件 tab: the optional chain entries can be switched off; the
                 safety baseline is shown locked (the backend refuses to store a
                 required name as disabled). -->
            <TabPanel
              value="agentMiddlewares"
              :header="t('config.agent.tabs.middlewares')"
              data-test="persona-agent-middlewares-tab">
              <div
                class="flex min-h-0 min-w-0 flex-1 flex-col gap-2"
                data-test="agent-middlewares-tab">
                <span class="text-sm text-gray-500 dark:text-gray-400">
                  {{ t('config.agent.middlewares.hint') }}
                </span>
                <div class="min-h-0 min-w-0 flex-1 overflow-y-auto pr-1">
                  <div
                    v-for="entry in agentStore.middlewares.gateable"
                    :key="entry.name"
                    class="mb-1 flex items-center justify-between gap-2 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
                    <div class="min-w-0">
                      <div class="truncate text-sm text-theme-main">
                        {{ t(`config.agent.middleware.${entry.name}.name`) }}
                      </div>
                      <div class="truncate text-xs text-gray-400">
                        {{ t(`config.agent.middleware.${entry.name}.desc`) }}
                      </div>
                    </div>
                    <ToggleSwitch
                      :model-value="agentMiddlewareEnabled(entry.name)"
                      :data-test="`agent-middleware-${entry.name}`"
                      @update:model-value="toggleMiddleware(entry.name)" />
                  </div>
                  <!-- Required, yet configurable: a REQUIRED middleware cannot be
                       switched off, so it exposes its own options here instead. -->
                  <div
                    class="mb-1 mt-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]"
                    data-test="agent-middleware-option-Summarization">
                    <div class="flex items-center justify-between gap-2">
                      <div class="min-w-0">
                        <div class="flex items-center gap-1 truncate text-sm text-theme-main">
                          <i class="pi pi-lock text-[9px] text-gray-400" />
                          {{ t('config.agent.middleware.Summarization.name') }}
                        </div>
                        <div class="truncate text-xs text-gray-400">
                          {{ t('config.agent.middlewares.nudgeDesc') }}
                        </div>
                      </div>
                      <label class="flex shrink-0 items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300">
                        {{ t('config.agent.middlewares.nudgeLabel') }}
                        <ToggleSwitch
                          :model-value="agentNudgeActive"
                          :disabled="!agentMemoryToolOn"
                          data-test="agent-middleware-nudge"
                          @update:model-value="agentNudge = $event" />
                      </label>
                    </div>
                    <div
                      v-if="!agentMemoryToolOn"
                      class="mt-1 text-[11px] text-amber-600 dark:text-amber-400"
                      data-test="agent-nudge-blocked">
                      {{ t('config.agent.middlewares.nudgeNeedMemory') }}
                    </div>
                  </div>
                  <div class="mt-3">
                    <div class="mb-1 text-xs font-semibold text-gray-500 dark:text-gray-400">
                      {{ t('config.agent.middlewares.lockedTitle') }}
                    </div>
                    <div class="flex flex-wrap gap-1">
                      <span
                        v-for="entry in agentStore.middlewares.locked"
                        :key="entry.name"
                        class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500 dark:bg-gray-800 dark:text-gray-400"
                        :title="t('config.agent.middlewares.lockedHint')">
                        <i class="pi pi-lock text-[9px]" />
                        {{ entry.name }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </TabPanel>

            <!-- 代理模型 tab: the main agent's model and one picker per functional
                 subagent role. Both lists are the environment-config model
                 profiles (the same list the session-model picker uses). -->
            <TabPanel
              value="agentSubagents"
              :header="t('config.agent.tabs.subagents')"
              data-test="persona-agent-subagents-tab">
              <div
                class="flex min-h-0 min-w-0 flex-1 flex-col gap-2"
                data-test="agent-models-tab">
                <div class="flex items-center gap-1">
                  <button
                    v-for="scope in AGENT_MODEL_SCOPES"
                    :key="scope"
                    type="button"
                    class="rounded-full px-2.5 py-0.5 text-xs transition-colors"
                    :class="
                      modelScope === scope
                        ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
                        : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                    "
                    :data-test="`agent-models-scope-${scope}`"
                    @click="modelScope = scope">
                    {{ t(`config.agent.models.scope_${scope}`) }}
                  </button>
                </div>
                <template v-if="modelScope === 'main'">
                  <span class="text-sm text-gray-500 dark:text-gray-400">
                    {{ t('config.agent.models.mainHint') }}
                  </span>
                  <Select
                    :model-value="agentMainModelId"
                    :options="agentMainModelOptions"
                    option-label="label"
                    option-value="value"
                    class="w-full"
                    data-test="agent-main-model"
                    @update:model-value="agentMainModel = String($event ?? '')" />
                </template>
                <template v-else>
                  <span class="text-sm text-gray-500 dark:text-gray-400">
                    {{ t('config.agent.subagents.hint') }}
                  </span>
                  <div class="min-h-0 min-w-0 flex-1 overflow-y-auto pr-1">
                    <div
                      v-for="role in agentStore.subagentRoles"
                      :key="role.role"
                      class="mb-2 flex items-center justify-between gap-3 rounded-lg border border-solid border-gray-light px-3 py-2 dark:border-[#555]">
                      <div class="min-w-0">
                        <div class="truncate text-sm text-theme-main">
                          {{ t(`config.agent.role.${role.role}`) }}
                          <span class="ml-1 font-mono text-[11px] text-gray-400">{{ role.role }}</span>
                        </div>
                        <div class="truncate text-xs text-gray-400">
                          {{
                            role.model_tier
                              ? t('config.agent.subagents.tier', { tier: role.model_tier })
                              : t('config.agent.subagents.tierDepth')
                          }}
                        </div>
                      </div>
                      <Select
                        :model-value="agentRoleModelId(role.role)"
                        :options="agentRoleModelOptions"
                        option-label="label"
                        option-value="value"
                        class="w-56"
                        :data-test="`agent-role-model-${role.role}`"
                        @update:model-value="value => setRoleModel(role.role, value)" />
                    </div>
                  </div>
                </template>
              </div>
            </TabPanel>

            <!-- 技能 tab: which skills' index entries (`<available_skills>` in the
                 system prompt) this preset keeps. Two sub-tabs split the shipped
                 skills from the uploaded third-party ones. -->
            <TabPanel
              value="agentSkills"
              :header="t('config.agent.tabs.skills')"
              data-test="persona-agent-skills-tab">
              <div
                class="flex min-h-0 min-w-0 flex-1 flex-col gap-2"
                data-test="agent-skills-tab">
                <div class="flex flex-wrap items-center justify-between gap-x-2 gap-y-1">
                  <span class="min-w-0 flex-1 text-sm text-gray-500 dark:text-gray-400">
                    {{ t('config.agent.skills.hint') }}
                  </span>
                  <div class="flex shrink-0 items-center gap-2">
                    <span class="text-xs text-gray-400">
                      {{
                        t('config.agent.skills.count', {
                          selected: agentEnabledSkills.length,
                          total: agentStore.catalog.skills.length
                        })
                      }}
                    </span>
                    <Button
                      :label="t('config.agent.tools.selectAll')"
                      severity="secondary"
                      text
                      size="small"
                      data-test="agent-skills-all"
                      @click="selectAllSkills" />
                    <Button
                      :label="t('config.agent.tools.clearAll')"
                      severity="secondary"
                      text
                      size="small"
                      data-test="agent-skills-none"
                      @click="clearAllSkills" />
                  </div>
                </div>
                <div class="flex items-center gap-1">
                  <button
                    v-for="scope in AGENT_SKILL_SCOPES"
                    :key="scope"
                    type="button"
                    class="rounded-full px-2.5 py-0.5 text-xs transition-colors"
                    :class="
                      skillsScope === scope
                        ? 'bg-[#c1d6e5] text-theme-main dark:bg-[#41556b]'
                        : 'bg-gray-100 text-gray-500 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-400'
                    "
                    :data-test="`agent-skills-scope-${scope}`"
                    @click="skillsScope = scope">
                    {{ t(`config.agent.skills.scope_${scope}`) }}
                  </button>
                </div>
                <div class="min-h-0 min-w-0 flex-1 overflow-y-auto pr-1">
                  <div
                    v-if="agentVisibleSkills.length === 0"
                    class="px-3 py-1 text-xs text-gray-400"
                    data-test="agent-skills-empty">
                    {{ t('config.agent.skills.empty') }}
                  </div>
                  <label
                    v-for="skill in agentVisibleSkills"
                    :key="skill.name"
                    class="flex items-center gap-1.5 py-0.5 text-xs text-gray-600 dark:text-gray-300"
                    :class="skill.required === true ? 'cursor-default' : 'cursor-pointer'"
                    :title="skillTooltip(skill)">
                    <Checkbox
                      v-if="skill.required !== true"
                      :model-value="agentSkillSelected(skill.name)"
                      binary
                      :data-test="`agent-skill-${skill.name}`"
                      @update:model-value="toggleSkill(skill.name)" />
                    <i
                      v-if="skill.required === true"
                      class="pi pi-lock text-[9px] text-gray-400" />
                    <span
                      class="font-mono"
                      :class="{
                        'text-gray-400 line-through': skill.required !== true && !agentSkillSelected(skill.name)
                      }"
                      :data-test="skill.required === true ? `agent-skill-locked-${skill.name}` : undefined">
                      {{ skill.name }}
                    </span>
                  </label>
                </div>
              </div>
            </TabPanel>
          </TabView>
          <div class="flex min-w-[420px] justify-end">
            <Button
              :label="t('config.persona.preset.savePreset')"
              icon="pi pi-save"
              severity="secondary"
              :loading="saving"
              :disabled="!actionEnabled"
              @click="savePreset" />
          </div>
        </template>
      </div>

      <!-- Right column: persona preset list -->
      <div class="flex w-full min-h-0 flex-col gap-2 md:w-[300px] md:shrink-0">
        <span class="text-sm font-semibold">{{ t('config.persona.preset.title') }}</span>
        <div class="flex max-h-[60vh] min-h-0 flex-1 flex-col gap-1 overflow-y-auto md:max-h-none">
          <!-- Virtual read-only entries: the built-in personas (no delete button,
               not Dexie rows) — 纯净 (nothing named, no persona content, required
               tools only), 编程助手 (the default: operating rules only, no soul /
               user profile), 情感陪伴 (full persona, orchestration off) and 全量
               (full persona, everything on). -->
          <div
            v-for="builtin in BUILTIN_PRESETS"
            :key="builtin.id"
            role="button"
            tabindex="0"
            class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light bg-white px-3 py-2 text-sm text-theme-main transition-colors dark:border-[#555] dark:bg-[#2a2a36]/[0.6]"
            :class="{
              'bg-[#c1d6e5]!': activeBuiltin === builtin.id,
              'md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]': activeBuiltin !== builtin.id
            }"
            :data-test="`builtin-${builtin.id}`"
            @click="selectBuiltin(builtin.id)">
            <span class="truncate">{{ t(builtin.nameKey) }}</span>
            <span
              class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t(builtin.badgeKey) }}
            </span>
          </div>
          <div
            v-if="presets.length === 0"
            class="px-3 py-1 text-xs text-gray-400">
            {{ t('config.persona.preset.emptyList') }}
          </div>
          <div
            v-for="preset in presets"
            :key="preset.id"
            role="button"
            tabindex="0"
            class="flex cursor-pointer items-center justify-between gap-2 rounded-lg border border-solid border-gray-light bg-white px-3 py-2 text-sm text-theme-main transition-colors dark:border-[#555] dark:bg-[#2a2a36]/[0.6]"
            :class="{
              'bg-[#c1d6e5]!': editingPresetId === preset.id,
              'md:hover:bg-[#e4efff] md:dark:hover:bg-[#c1d6e5]': editingPresetId !== preset.id
            }"
            @click="selectPreset(preset)">
            <span class="truncate">{{ preset.name }}</span>
            <span
              v-if="editingPresetId === preset.id"
              class="shrink-0 rounded-full bg-gray-200 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-700 dark:text-gray-300">
              {{ t('config.persona.preset.editingBadge') }}
            </span>
            <Button
              class="shrink-0"
              icon="pi pi-trash"
              severity="danger"
              text
              rounded
              size="small"
              :aria-label="t('common.delete')"
              @click.stop="requestDeletePreset(preset)" />
          </div>
        </div>
        <Button
          :label="t('config.persona.preset.apply')"
          icon="pi pi-check"
          class="w-full"
          :loading="applying"
          :disabled="!actionEnabled"
          @click="handleApply" />
      </div>
    </div>

    <!-- Name dialog: save current content as a new preset -->
    <Dialog
      v-model:visible="showNameDialog"
      :header="t('config.persona.preset.nameDialog.title')"
      :modal="true"
      :closable="true"
      :style="{ width: '360px' }">
      <div class="flex flex-col gap-2">
        <InputText
          v-model="presetName"
          :placeholder="t('config.persona.preset.nameDialog.placeholder')"
          :maxlength="50"
          @keydown.enter="confirmSavePreset" />
        <small
          v-if="nameError === 'duplicate'"
          class="text-red-500">
          {{ t('config.persona.preset.nameError.duplicate') }}
        </small>
        <small
          v-else-if="nameError === 'required'"
          class="text-red-500">
          {{ t('config.persona.preset.nameError.required') }}
        </small>
      </div>
      <template #footer>
        <div class="flex justify-end gap-2">
          <Button
            :label="t('config.cancel')"
            severity="secondary"
            @click="showNameDialog = false" />
          <Button
            :label="t('config.persona.preset.nameDialog.confirm')"
            icon="pi pi-save"
            :loading="saving"
            :disabled="!presetName.trim()"
            @click="confirmSavePreset" />
        </div>
      </template>
    </Dialog>

    <!-- Avatar crop (role tab): 1:1, output 512×512 — moved here with the role config -->
    <AvatarCropDialog
      v-model="cropVisible"
      :src="cropSource"
      :aspect-ratio="1"
      :output-width="512"
      :output-height="512"
      :header="cropVisible ? t('config.crop.title') : ''"
      @cropped="onCropConfirmed" />
  </div>
</template>

<script lang="ts" setup>
import { ref, computed, onMounted } from 'vue';
import { useI18n } from 'vue-i18n';
import AvatarCropDialog from './AvatarCropDialog.vue';
import type { AgentConfig, PersonaPreset, PresetCharacter } from '@/composables/db';
import type { SessionModelProfile } from '~/composables/bridge/session';
import { FOLLOW_TIER_ID, isBulkOnlyToolGroup } from '~/stores/agent-config';
import { ENV_MODEL_ID } from '~/stores/session-model';
// DEFAULT_CACHED_CHARACTER / DEFAULT_PLACEHOLDER_AVATAR are auto-imported from
// ~/composables/defaultCharacter.
import { logUtil } from '~/utils/log';

const { t, locale } = useI18n({ useScope: 'local' });

const emits = defineEmits<{ saved: [] }>();

const MAX_CHARS = 2000;

interface PersonaTab {
  /** Unique key for the tab; the same basename may exist across layers. */
  key: string;
  /** Actual filename sent to the backend (e.g. 'SOUL.md'). */
  file: string;
  i18nKey: string;
  i18nDescKey: string;
  /** Read function that returns a file->content map for this tab's group. */
  readFn: () => Promise<Record<string, string>>;
}

const tabs: PersonaTab[] = [
  {
    key: 'AGENTS.md',
    file: 'AGENTS.md',
    i18nKey: 'config.tabs.agents',
    i18nDescKey: 'config.desc.agents',
    readFn: readSystemPrompt
  },
  {
    key: 'SOUL.md',
    file: 'SOUL.md',
    i18nKey: 'config.tabs.soul',
    i18nDescKey: 'config.desc.soul',
    readFn: readSystemPrompt
  },
  {
    key: 'USER.md',
    file: 'USER.md',
    i18nKey: 'config.tabs.user',
    i18nDescKey: 'config.desc.user',
    readFn: readSystemPrompt
  }
] as const;

/** The role statement file the role tab owns (composed, not a textarea tab). */
const ROLE_FILE = 'ROLE.md';

const activeTab = ref(0);
const loading = ref(false);
const saving = ref(false);
const restoring = ref(false);
const applying = ref(false);
const editContent = ref<Record<string, string>>({});
const originalContent = ref<Record<string, string>>({});

// ── Role config (the first tab): both role names + avatars ─────────────────
// The names are display info (Dexie global profile, locked per session on first
// open) AND prompt content: 应用 composes ROLE.md from them in the active UI
// language, so the agent reads who it plays and who the user plays.
/** One catalogue tool-group row the 工具 tab renders. */
interface AgentToolGroupEntry {
  group: string;
  tools: Array<{ name: string; description?: string; required?: boolean }>;
}

// ── 工具 / 中间件 / 子代理模型 drafts (the preset's `agent` block) ──────────
// The catalog (tool groups, middleware lock flags, roles) comes from the backend
// once; the drafts below are what 保存预设 stores and 应用 writes to the session.
const agentStore = useAgentConfigStore();
const llmProfiles = useLlmProfilesStore();
/** The session's main-model control (its own store: the toolbar picker's key). */
const sessionModel = useSessionModelStore();
const route = useRoute();
/** The session an 应用 targets (the open chat's sid; empty on the bare shell). */
const panelSessionId = computed(() => (typeof route.params.sid === 'string' ? route.params.sid : ''));

/** Enabled tool names (catalog order); the draft starts all-selected. */
const agentTools = ref<string[]>([]);
/** Gateable middleware names turned OFF. */
const agentDisabledMiddlewares = ref<string[]>([]);
/** role → chosen env-config profile id (`''` = follow the role tier). */
const agentRoleModels = ref<Record<string, string>>({});
/** Skill names whose index entries enter the prompt (`null` = every skill). */
const agentSkills = ref<string[] | null>(null);
/** Main-agent model for this preset (`ENV_MODEL_ID` = follow the env config). */
const agentMainModel = ref<string>(ENV_MODEL_ID);
/** Whether the summarization nudges run (the Summarization middleware option). */
const agentNudge = ref(true);

/** Required tool names from the catalogue, in catalogue order. */
const agentRequiredToolNames = computed<string[]>(() =>
  agentStore.catalog.tools.filter(tool => tool.required === true).map(tool => tool.name)
);

/** Tool names currently enabled, in catalogue order (required tools always on). */
const agentEnabledTools = computed<string[]>(() => {
  const all = agentStore.catalog.tools.map(tool => tool.name);
  const required = new Set(agentRequiredToolNames.value);
  if (agentTools.value.length === 0) return all;
  const selected = new Set(agentTools.value);
  return all.filter(name => selected.has(name) || required.has(name));
});

/** Select-options: "follow the role tier" + every env-config profile. */
const agentRoleModelOptions = computed<Array<{ label: string; value: string }>>(() => [
  { label: t('config.agent.subagents.followTier'), value: FOLLOW_TIER_ID },
  ...llmProfiles.listFor('MAIN_LLM').map(profile => ({ label: profile.label, value: profile.id }))
]);

/** Select-options: "follow the environment config" + every env-config profile. */
const agentMainModelOptions = computed<Array<{ label: string; value: string }>>(() => [
  { label: t('config.agent.models.followEnv'), value: ENV_MODEL_ID },
  ...llmProfiles.listFor('MAIN_LLM').map(profile => ({ label: profile.label, value: profile.id }))
]);

/** The profile id the 主代理 picker shows (an unknown id falls back to env). */
const agentMainModelId = computed<string>(() =>
  llmProfiles.byId('MAIN_LLM', agentMainModel.value) ? agentMainModel.value : ENV_MODEL_ID
);

/** Whether the memory tool is on in this draft (the nudge's precondition). */
const agentMemoryToolOn = computed<boolean>(() => agentToolSelected(MEMORY_TOOL_NAME));

/** The nudge switch's effective state: off without the memory tool. */
const agentNudgeActive = computed<boolean>(() => agentNudge.value && agentMemoryToolOn.value);

/**
 * The skill's profile descriptor for the main-model payload (null = env).
 * @returns The chosen profile, or null to follow the environment config.
 */
const agentMainModelProfile = (): SessionModelProfile | null => {
  const profile = llmProfiles.byId('MAIN_LLM', agentMainModelId.value);
  return profile ? llmProfiles.toSessionProfile(profile) : null;
};

/**
 * The hover text of one skill row: the SKILL.md description, plus the locked
 * explanation for a required skill.
 * @param skill Catalogue skill entry.
 * @param skill.name
 * @param skill.description
 * @param skill.required
 */
const skillTooltip = (skill: { name: string; description?: string; required?: boolean }): string => {
  const description = skill.description || skill.name;
  return skill.required === true ? `${description}\n${t('config.agent.tools.requiredHint')}` : description;
};

/**
 * Whether one tool is checked.
 * @param name
 */
const agentToolSelected = (name: string): boolean => agentEnabledTools.value.includes(name);

/**
 * Whether every tool of a group is checked.
 * @param group
 * @param group.tools
 */
const agentGroupFullySelected = (group: { tools: Array<{ name: string }> }): boolean =>
  group.tools.every(tool => agentToolSelected(tool.name));

/**
 * Whether a group is entirely required (its select-all toggle is pointless: the
 * membership can never change, so the header shows the locked hint instead).
 * @param group Catalogue group entry.
 * @param group.tools
 */
const agentGroupFullyRequired = (group: AgentToolGroupEntry): boolean =>
  group.tools.length > 0 && group.tools.every(tool => tool.required === true);

/** The two 工具 sub-tabs: the built-in groups and the MCP servers' tools. */
const AGENT_TOOL_SCOPES = ['builtin', 'mcp'] as const;

/** The two 技能 sub-tabs: shipped skills and uploaded third-party ones. */
const AGENT_SKILL_SCOPES = ['builtin', 'thirdparty'] as const;

/** The two 代理模型 sub-tabs: the main agent and the functional subagents. */
const AGENT_MODEL_SCOPES = ['main', 'subagent'] as const;

/** Which 代理模型 sub-tab is on screen. */
const modelScope = ref<(typeof AGENT_MODEL_SCOPES)[number]>('main');

/** Group id the catalogue gives tools that arrive from an MCP server. */
const MCP_TOOL_GROUP = 'mcp';

/** Tool the nudge option depends on (it writes memory through it). */
const MEMORY_TOOL_NAME = 'memory';

/** Which 工具 sub-tab is on screen (a view filter — the draft stays whole). */
const toolScope = ref<(typeof AGENT_TOOL_SCOPES)[number]>('builtin');

/** Which 技能 sub-tab is on screen. */
const skillsScope = ref<(typeof AGENT_SKILL_SCOPES)[number]>('builtin');

/** The tool groups of the active sub-tab. */
const agentVisibleToolGroups = computed<AgentToolGroupEntry[]>(() =>
  agentStore.toolGroups.filter(group =>
    toolScope.value === MCP_TOOL_GROUP ? group.group === MCP_TOOL_GROUP : group.group !== MCP_TOOL_GROUP
  )
);

/** The skills of the active sub-tab. */
const agentVisibleSkills = computed(() =>
  skillsScope.value === 'builtin' ? agentStore.skills.builtin : agentStore.skills.thirdParty
);

/**
 * Whether a row carries its own switch: a required tool is locked on (no
 * checkbox at all) and a bulk-only group moves as a whole through its header
 * toggle only.
 * @param group Catalogue group entry.
 * @param group.tools
 * @param tool Catalogue tool entry.
 * @param tool.required
 */
const agentToolSwitchVisible = (group: AgentToolGroupEntry, tool: { required?: boolean }): boolean =>
  tool.required !== true && !isBulkOnlyToolGroup(group.group);

/**
 * The ``data-test`` of a tool row's chip (switch rows carry it on the checkbox).
 * @param group Catalogue group entry.
 * @param group.tools
 * @param tool Catalogue tool entry.
 * @param tool.name
 * @param tool.required
 * @returns The chip's test id, or ``undefined`` for a switch row.
 */
const agentToolChipTestId = (
  group: AgentToolGroupEntry,
  tool: { name: string; required?: boolean }
): string | undefined => {
  if (tool.required === true) return `agent-tool-locked-${tool.name}`;
  return isBulkOnlyToolGroup(group.group) ? `agent-tool-bulk-${tool.name}` : undefined;
};

/** Skill names the draft keeps (required always in; every skill with no opinion). */
const agentEnabledSkills = computed<string[]>(() => {
  const all = agentStore.catalog.skills;
  if (agentSkills.value === null) return all.map(skill => skill.name);
  const selected = new Set(agentSkills.value);
  return all.filter(skill => selected.has(skill.name) || skill.required === true).map(skill => skill.name);
});

/**
 * Whether one skill is checked.
 * @param name
 */
const agentSkillSelected = (name: string): boolean => agentEnabledSkills.value.includes(name);

/**
 * Toggle one skill's index entry (the draft starts as "no opinion" = all on).
 * @param name Skill name.
 */
const toggleSkill = (name: string): void => {
  if (agentStore.catalog.skills.some(skill => skill.name === name && skill.required === true)) return;
  const current = agentEnabledSkills.value;
  agentSkills.value = current.includes(name) ? current.filter(candidate => candidate !== name) : [...current, name];
};

/** Keep every skill in the index (the draft collapses back to "no opinion"). */
const selectAllSkills = (): void => {
  agentSkills.value = null;
};

/** Drop every skill's index entry (an explicit, legal empty selection). */
const clearAllSkills = (): void => {
  agentSkills.value = [];
};

/**
 * The hover text of one tool row: the backend description, plus the locked
 * explanation for a required tool.
 * @param tool Catalogue tool entry.
 * @param tool.name
 * @param tool.description
 * @param tool.required
 */
const toolTooltip = (tool: { name: string; description?: string; required?: boolean }): string => {
  const description = tool.description || tool.name;
  return tool.required === true ? `${description}\n${t('config.agent.tools.requiredHint')}` : description;
};

/**
 * Whether a gateable middleware runs for this draft.
 * @param name
 */
const agentMiddlewareEnabled = (name: string): boolean => !agentDisabledMiddlewares.value.includes(name);

/**
 * Toggle one tool (a required tool is locked on and ignores the click).
 * @param name Tool name.
 */
const toggleTool = (name: string): void => {
  if (agentRequiredToolNames.value.includes(name)) return;
  const current = agentEnabledTools.value;
  agentTools.value = current.includes(name) ? current.filter(candidate => candidate !== name) : [...current, name];
};

/**
 * Select / clear a whole group (clearing never drops a required tool).
 * @param groupEntry Catalogue group entry.
 */
const toggleToolGroup = (groupEntry: AgentToolGroupEntry): void => {
  const names = groupEntry.tools.map(tool => tool.name);
  if (agentGroupFullySelected(groupEntry)) {
    const required = new Set(agentRequiredToolNames.value);
    agentTools.value = agentEnabledTools.value.filter(name => !names.includes(name) || required.has(name));
    return;
  }
  agentTools.value = [...new Set([...agentEnabledTools.value, ...names])];
};

/** Check every catalogue tool. */
const selectAllTools = (): void => {
  agentTools.value = agentStore.catalog.tools.map(tool => tool.name);
};

/** Uncheck every optional tool (the required set stays on). */
const clearAllTools = (): void => {
  agentTools.value = [...agentRequiredToolNames.value];
};

/**
 * Toggle one gateable middleware.
 * @param name Middleware name.
 */
const toggleMiddleware = (name: string): void => {
  agentDisabledMiddlewares.value = agentDisabledMiddlewares.value.includes(name)
    ? agentDisabledMiddlewares.value.filter(candidate => candidate !== name)
    : [...agentDisabledMiddlewares.value, name];
};

/**
 * The profile id a role's picker shows (`''` = follow the tier).
 * @param role
 */
const agentRoleModelId = (role: string): string => agentRoleModels.value[role] || FOLLOW_TIER_ID;

/**
 * Point one role at an env-config profile (or back at its tier).
 * @param role Functional role.
 * @param profileId Chosen profile id (`''` = follow the tier).
 */
const setRoleModel = (role: string, profileId: string): void => {
  const chosen = String(profileId ?? '');
  agentRoleModels.value = {
    ...agentRoleModels.value,
    [role]: chosen === FOLLOW_TIER_ID ? '' : chosen
  };
};

/**
 * Build the payload from the drafts.
 * @returns The `agent` block ({} when everything is at its default).
 */
const buildAgentDraft = (): AgentConfig => {
  const allTools = agentStore.catalog.tools.map(tool => tool.name);
  const everyToolOn = agentEnabledTools.value.length === allTools.length;
  const models: Record<string, SessionModelProfile | null> = {};
  for (const role of agentStore.subagentRoles) {
    const profileId = agentRoleModels.value[role.role] ?? '';
    if (!profileId) {
      models[role.role] = null;
      continue;
    }
    const profile = llmProfiles.byId('MAIN_LLM', profileId);
    models[role.role] = profile ? llmProfiles.toSessionProfile(profile) : null;
  }
  const anyModel = Object.values(models).some(value => value !== null);
  const block: AgentConfig = {};
  if (!everyToolOn) block.tools = agentEnabledTools.value;
  if (agentDisabledMiddlewares.value.length > 0) {
    block.middlewares_disabled = [...agentDisabledMiddlewares.value];
  }
  if (anyModel) block.subagent_models = models;
  // `null` = no opinion (every skill stays in the index) and collapses away; an
  // explicit list — including the empty "only the required chain" choice — is
  // stored as-is.
  if (agentSkills.value !== null) block.skills = [...agentSkills.value];
  // Required middlewares have no on/off switch, so the nudge rides its own
  // options section — and only when the user turned it OFF (absent = the
  // default: on). The memory-tool gate is a DISPLAY/run-time condition: a draft
  // without the memory tool stores nothing extra, so re-enabling the tool brings
  // the nudge back instead of silently pinning it off.
  if (!agentNudge.value) {
    block.middleware_options = { Summarization: { nudge: false } };
  }
  return block;
};

/**
 * Load an `agent` block into the drafts (a preset selection, a session hydrate).
 * @param block Stored / effective config ({} = every default).
 */
const fillAgentDraft = (block: AgentConfig | undefined): void => {
  const allTools = agentStore.catalog.tools.map(tool => tool.name);
  const configured = Array.isArray(block?.tools) ? block?.tools : null;
  agentTools.value = configured ? allTools.filter(name => configured.includes(name)) : [...allTools];
  agentDisabledMiddlewares.value = [...(block?.middlewares_disabled ?? [])];
  const models: Record<string, string> = {};
  for (const [role, profile] of Object.entries(block?.subagent_models ?? {})) {
    models[role] = profile?.id ?? '';
  }
  agentRoleModels.value = models;
  agentSkills.value = Array.isArray(block?.skills) ? [...block.skills] : null;
  agentNudge.value = block?.middleware_options?.Summarization?.nudge !== false;
};

const charAssistant = ref({ name: DEFAULT_CACHED_CHARACTER.aiName, avatar: DEFAULT_CACHED_CHARACTER.aiAvatar });
const charUser = ref({ name: DEFAULT_CACHED_CHARACTER.userName, avatar: DEFAULT_CACHED_CHARACTER.userAvatar });
/** Snapshot of the loaded character (Dexie writes only happen when something changed). */
const originalChar = ref<PresetCharacter>({
  aiName: DEFAULT_CACHED_CHARACTER.aiName,
  aiAvatar: DEFAULT_CACHED_CHARACTER.aiAvatar,
  userName: DEFAULT_CACHED_CHARACTER.userName,
  userAvatar: DEFAULT_CACHED_CHARACTER.userAvatar
});

// Avatar crop state (1:1; the crop dialog itself is shared with the background editor)
const cropVisible = ref(false);
const cropSource = ref('');
const cropTarget = ref<'user' | 'assistant'>('user');

// Preset state machine: editingPresetId = the user preset currently loaded into the
// editor (null = built-in / new mode); activeBuiltin = which virtual built-in entry
// is loaded. The DEFAULT preset (全量) starts highlighted; the list itself
// (BUILTIN_PRESETS + the saved presets) is defined in the shared catalogue, so the
// new-session dialog and the top-bar viewer render the identical order and badges.
const editingPresetId = ref<number | null>(null);
const activeBuiltin = ref<BuiltinPresetId | null>(DEFAULT_PRESET_ID);

// Name dialog (save current content as a new preset) state.
const showNameDialog = ref(false);
const presetName = ref('');
const nameError = ref<'' | 'duplicate' | 'required'>('');

// Shared preset list singleton (auto-refreshes on first use).
const { presets, create, update, remove } = usePersonaPresets();

const onDialogShow = () => {
  void loadContent();
  editingPresetId.value = null;
  activeBuiltin.value = 'sherry';
  showNameDialog.value = false;
  presetName.value = '';
};

// The panel's mount lifetime drives the load (replaces the dialog's @show).
onMounted(onDialogShow);

const loadContent = async () => {
  loading.value = true;
  try {
    // Group tabs by their read function so each group is fetched once.
    const byReadFn = new Map<PersonaTab['readFn'], PersonaTab[]>();
    for (const tab of tabs) {
      const list = byReadFn.get(tab.readFn) ?? [];
      list.push(tab);
      byReadFn.set(tab.readFn, list);
    }
    const content: Record<string, string> = {};
    for (const [readFn, groupTabs] of byReadFn) {
      const data = await readFn();
      for (const tab of groupTabs) {
        content[tab.key] = data[tab.file] ?? '';
      }
    }
    editContent.value = { ...content };
    originalContent.value = { ...content };

    // The three agent tabs: the catalogue first (the drafts are expressed in
    // catalogue order), then the OPEN session's own config when there is one
    // (a session-less panel — the bare shell — edits the every-default draft).
    await agentStore.loadCatalog();
    if (panelSessionId.value) {
      await agentStore.hydrate(panelSessionId.value);
      fillAgentDraft(agentStore.configOf(panelSessionId.value));
    } else {
      fillAgentDraft({});
    }

    // Role names/avatars come from the local Dexie global profile (the same row
    // the old 系统配置-角色配置 tab edited); ROLE.md itself is NOT parsed back —
    // the compose direction is one-way (names -> sentence).
    const charData = await readCachedCharacter(GLOBAL_SESSION_KEY);
    // An explicitly EMPTY name/avatar stays empty (the input shows its
    // placeholder): the nameless built-ins (纯净 / 编程助手) ship no names on
    // purpose, and prefilling the shipped character here made the panel disagree
    // with the preset it had just applied.
    fillCharacter({
      aiName: charData?.aiName ?? DEFAULT_CACHED_CHARACTER.aiName,
      aiAvatar: charData?.aiAvatar ?? DEFAULT_CACHED_CHARACTER.aiAvatar,
      userName: charData?.userName ?? DEFAULT_CACHED_CHARACTER.userName,
      userAvatar: charData?.userAvatar ?? DEFAULT_CACHED_CHARACTER.userAvatar
    });
    originalChar.value = buildPresetCharacter();
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to load content:', e);
  } finally {
    loading.value = false;
  }
};

/**
 * Gate for 保存预设 / 应用: every file tab within the char limit. Empty content
 * is LEGAL — the 编程助手 built-in ships an empty soul / user profile (a plain
 * assistant without role-play) and an empty user role name means "the user
 * plays nothing" — so no non-empty requirement remains. The backend accepts
 * blank persona writes for the same reason.
 */
const allTabsValid = computed(() => tabs.every(tab => (editContent.value[tab.file] ?? '').length <= MAX_CHARS));

/** Enablement for both the 保存预设 ("Save Preset") and 应用 ("Apply") buttons. */
const actionEnabled = computed(
  () => allTabsValid.value && !loading.value && !saving.value && !applying.value && !restoring.value
);

/**
 * The role names + avatars as one snapshot (the preset's `character` block).
 * @returns The current role state.
 */
const buildPresetCharacter = (): PresetCharacter => ({
  aiName: charAssistant.value.name,
  aiAvatar: charAssistant.value.avatar,
  userName: charUser.value.name,
  userAvatar: charUser.value.avatar
});

/**
 * Current role state from a preset / the defaults / the loaded profile.
 * @param character Role names + avatars to load into the tab.
 */
const fillCharacter = (character: PresetCharacter) => {
  charAssistant.value = { name: character.aiName, avatar: character.aiAvatar };
  charUser.value = { name: character.userName, avatar: character.userAvatar };
};

/**
 * File map for the persona API: the edited files + the composed role file
 * (`composeRoleFile` in the shared catalogue states the roles, in the current UI
 * language, and omits a blank name entirely).
 * @returns The full apply payload written through `writeSystemPrompt`.
 */
const buildApplyContent = (): Record<string, string> => {
  const fileToContent: Record<string, string> = {};
  for (const tab of tabs) {
    fileToContent[tab.file] = editContent.value[tab.key] ?? '';
  }
  fileToContent[ROLE_FILE] = composeRoleFile(buildPresetCharacter(), t);
  return fileToContent;
};

/**
 * File map stored in a preset: only the files the user edits. ROLE.md is
 * recomposed from the preset's `character` on apply, so a stale sentence can
 * never be replayed (and never drifts from the names).
 * @returns The file map stored in a persona preset.
 */
const buildPresetContent = (): Record<string, string> => {
  const fileToContent: Record<string, string> = {};
  for (const tab of tabs) {
    fileToContent[tab.file] = editContent.value[tab.key] ?? '';
  }
  return fileToContent;
};

/**
 * Fill all file tabs with the given file->content map.
 * @param content File basename -> content (extra files, e.g. ROLE.md, are ignored here).
 * @returns Nothing.
 */
const fillTabs = (content: Record<string, string>) => {
  for (const tab of tabs) {
    editContent.value[tab.key] = content[tab.file] ?? '';
  }
};

/**
 * Localized label for the avatar FileUpload (`#filelabel` slot, replacing the
 * browser-native "No file chosen"): file selected → its name; otherwise the prompt.
 * @param files Selected files from the FileUpload event.
 * @returns The label text for the upload row.
 */
const avatarFileLabel = (files: File[]): string => {
  if (files.length > 0) return files[0]?.name ?? '';
  return t('config.role.noFileChosen');
};

/**
 * Reads an uploaded image file as a base64 data URL.
 * @param file Image file to read.
 * @returns The data URL, or '' when the reader produced no string.
 */
const readFileAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : '');
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });

const onUserAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  void openAvatarCrop('user', file);
};

const onAssistAvatarSelect = (event: { files: File[] }) => {
  const file = event.files?.[0];
  if (!file) return;
  void openAvatarCrop('assistant', file);
};

/**
 * Open the 1:1 crop dialog for an avatar pick.
 * @param target Which role the avatar belongs to.
 * @param file
 */
const openAvatarCrop = async (target: 'user' | 'assistant', file: File) => {
  try {
    cropTarget.value = target;
    cropSource.value = await readFileAsDataUrl(file);
    cropVisible.value = true;
  } catch (e) {
    logUtil.e('[PersonaPanel] Image read failed:', e);
  }
};

/**
 * Apply the crop result to the role the dialog was opened for.
 * @param dataUrl
 */
const onCropConfirmed = (dataUrl: string) => {
  if (cropTarget.value === 'user') {
    charUser.value.avatar = dataUrl;
  } else {
    charAssistant.value.avatar = dataUrl;
  }
  cropVisible.value = false;
};

/** Reset the role tab to the built-in default names + avatars (mirrors 恢复默认). */
const restoreRoleDefault = () => {
  fillCharacter({ ...DEFAULT_CACHED_CHARACTER });
};

/**
 * Clear ONE role's avatar back to the neutral placeholder ("reset to the
 * default avatar"): the field becomes empty — an empty avatar means "no avatar
 * of its own" and every render site falls back to the auto-imported
 * `DEFAULT_PLACEHOLDER_AVATAR` gray silhouette. The name is left untouched, so
 * this is the per-avatar sibling of 恢复默认.
 * @param target Which role the avatar belongs to.
 */
const resetAvatar = (target: 'user' | 'assistant') => {
  if (target === 'user') {
    charUser.value.avatar = '';
    return;
  }
  charAssistant.value.avatar = '';
};

const restoreDefault = async (tab: PersonaTab) => {
  restoring.value = true;
  try {
    const content = await readSystemPromptTemplate(locale.value);
    editContent.value[tab.key] = content[tab.file] ?? '';
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to restore default:', e);
  } finally {
    restoring.value = false;
  }
};

/**
 * Click a built-in entry: load its shipped template through the shared catalogue
 * (the same payload the new-session dialog and the top-bar viewer use).
 * - 纯净 → nobody named, no persona content, and only the required tools;
 * - 编程助手 → the operating rules only: empty soul and user profile, and BOTH
 *   role names empty — a plain coding assistant, no role statement at all;
 * - 情感陪伴 / 全量 → the full language template plus the default names/avatars
 *   (情感陪伴 also pins the orchestration surfaces off).
 * @param id Which built-in entry was clicked.
 */
const selectBuiltin = async (id: BuiltinPresetId) => {
  if (restoring.value || loading.value) return;
  restoring.value = true;
  try {
    // The catalogue feeds 纯净 / 情感陪伴's agent block; it is fetched once per
    // process (the panel already awaited it during load, so this is a cache hit).
    await agentStore.loadCatalog();
    const template = await readSystemPromptTemplate(locale.value);
    const payload = builtinPayload(id, template, t, presetCatalogFacts(agentStore), locale.value);
    fillTabs(payload.content);
    fillCharacter(payload.character);
    fillAgentDraft(payload.agent);
    agentMainModel.value = payload.mainModel?.id ?? ENV_MODEL_ID;
    editingPresetId.value = null;
    activeBuiltin.value = id;
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to load persona template:', e);
  } finally {
    restoring.value = false;
  }
};

/**
 * Click a user preset entry: load its content into all tabs and enter edit mode.
 * @param preset
 */
const selectPreset = (preset: PersonaPreset) => {
  if (loading.value || restoring.value || preset.id === undefined) return;
  fillTabs(preset.content);
  // Presets saved before the role tab existed carry no character block: leave
  // the current roles untouched rather than silently resetting them.
  if (preset.character) fillCharacter(preset.character);
  // Same tolerance for the agent block: an old preset = every default.
  fillAgentDraft(preset.agent);
  // Same tolerance for the model: an old preset leaves the picker at "follow env".
  agentMainModel.value = preset.main_model?.id ?? ENV_MODEL_ID;
  editingPresetId.value = preset.id;
  activeBuiltin.value = null;
};

/**
 * Click 保存预设 ("Save Preset"):
 * - editing an existing preset → overwrite it directly (no name dialog);
 * - otherwise (default/new mode) → open the name dialog to save as a new preset.
 */
const savePreset = () => {
  if (!actionEnabled.value) return;
  if (editingPresetId.value !== null) {
    void overwriteEditingPreset();
  } else {
    presetName.value = '';
    nameError.value = '';
    showNameDialog.value = true;
  }
};

/** Direct-overwrite path for 保存预设 ("Save Preset") while editing a user preset. */
const overwriteEditingPreset = async () => {
  const id = editingPresetId.value;
  if (id === null) return;
  saving.value = true;
  try {
    const ok = await update(
      id,
      buildPresetContent(),
      buildPresetCharacter(),
      buildAgentDraft(),
      agentMainModelProfile()
    );
    if (ok) {
      toastSuccess(t('config.persona.preset.toast.presetSaved'));
    } else {
      toastError(t('config.persona.preset.toast.presetSaveFailed'));
    }
  } finally {
    saving.value = false;
  }
};

/** Confirm the name dialog: create a new preset (duplicate → inline error, dialog stays open). */
const confirmSavePreset = async () => {
  if (saving.value || !showNameDialog.value) return;
  const name = presetName.value.trim();
  if (!name) {
    nameError.value = 'required';
    return;
  }
  saving.value = true;
  try {
    const result = await create(
      name,
      buildPresetContent(),
      buildPresetCharacter(),
      buildAgentDraft(),
      agentMainModelProfile()
    );
    if (result.ok) {
      showNameDialog.value = false;
      // The just-created preset becomes the edited one (its entry is highlighted).
      editingPresetId.value = result.id;
      activeBuiltin.value = null;
      toastSuccess(t('config.persona.preset.toast.presetSaved'));
    } else if (result.reason === 'duplicate') {
      nameError.value = 'duplicate';
    } else {
      toastError(t('config.persona.preset.toast.presetSaveFailed'));
    }
  } finally {
    saving.value = false;
  }
};

// PrimeVue ConfirmationService (ConfirmDialog mounted in app.vue) — same pattern as SessionSidebar.
const confirm = useConfirm();

/**
 * Delete a preset after a second confirmation.
 * @param preset
 */
const requestDeletePreset = (preset: PersonaPreset) => {
  if (preset.id === undefined) return;
  confirm.require({
    header: t('config.persona.preset.confirmDelete.title'),
    message: t('config.persona.preset.confirmDelete.message', { name: preset.name }),
    acceptProps: { label: t('common.delete'), severity: 'danger', icon: 'pi pi-trash' },
    rejectProps: { label: t('config.cancel'), severity: 'secondary' },
    accept: () => {
      void doRemovePreset(preset);
    }
  });
};

/**
 * Actual delete executor (triggered by the confirmation dialog accept callback).
 * @param preset
 */
const doRemovePreset = async (preset: PersonaPreset) => {
  const id = preset.id;
  if (id === undefined) return;
  const ok = await remove(id);
  if (!ok) return;
  toastSuccess(t('config.persona.preset.toast.presetDeleted'));
  if (editingPresetId.value === id) {
    // The deleted preset was being edited: reset the edit state (no entry highlighted).
    editingPresetId.value = null;
    activeBuiltin.value = null;
  }
};

/**
 * Write the role names/avatars to the local Dexie global profile when they
 * changed — the "global pending profile" new sessions copy their display
 * snapshot from, exactly as the old 系统配置-角色配置 tab wrote it.
 */
const persistCharacter = async () => {
  const current = buildPresetCharacter();
  const changed =
    current.userName !== originalChar.value.userName ||
    current.userAvatar !== originalChar.value.userAvatar ||
    current.aiName !== originalChar.value.aiName ||
    current.aiAvatar !== originalChar.value.aiAvatar;
  if (!changed) return;
  await cacheCharacter({
    session_id: GLOBAL_SESSION_KEY,
    userName: current.userName,
    userAvatar: current.userAvatar,
    aiName: current.aiName,
    aiAvatar: current.aiAvatar
  });
  originalChar.value = current;
};

/** Click 应用 ("Apply"): full-write the persona files (incl. the composed ROLE.md) + the role profile; success → toast + saved + close, failure → stay open. */
const handleApply = async () => {
  if (!actionEnabled.value) return;
  applying.value = true;
  try {
    const snapshot = buildApplyContent();
    await writeSystemPrompt(snapshot);
    // Verify the write actually landed: in browser mode fetchApi swallows request
    // failures (retries 3x, then resolves null instead of throwing), so a failed
    // PUT would otherwise be indistinguishable from success here. Read the files
    // back and compare; any read failure or mismatch → treat the apply as failed.
    const written = await readSystemPrompt();
    const verified = !!written && Object.entries(snapshot).every(([file, content]) => written[file] === content);
    if (!verified) {
      throw new Error('[PersonaPanel] applied content verification failed');
    }
    await persistCharacter();
    // The agent config lands on the SESSION (this preset's 工具 / 中间件 /
    // 子代理模型 go live for the open chat; the backend parks it until the turn
    // boundary when one is in flight). A session-less panel skips it.
    if (panelSessionId.value) {
      await agentStore.save(panelSessionId.value, buildAgentDraft());
      // The main model rides its own endpoint (the session-model control owns
      // that key); the write parks mid-turn exactly like the config above.
      await sessionModel.select(panelSessionId.value, agentMainModelProfile());
    }
    emits('saved');
    toastSuccess(t('config.persona.preset.toast.applySuccess'));
  } catch (e) {
    logUtil.e('[PersonaPanel] Failed to apply persona:', e);
    toastError(t('config.persona.preset.toast.applyFailed'));
  } finally {
    applying.value = false;
  }
};
</script>

<i18n lang="json">
{
  "zh": {
    "config": {
      "persona": {
        "restoreDefault": "恢复默认",
        "preset": {
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
          }
        }
      },
      "desc": {
        "role": "AI 与用户各自的扮演角色（名字与头像）",
        "agents": "Agent 的运行守则与安全边界",
        "soul": "Agent人格、语气、性格",
        "user": "用户信息和偏好"
      },
      "uploadAvatar": "上传头像",
      "crop": {
        "title": "裁剪头像"
      },
      "role": {
        "charNote": "名字与头像修改仅在新建会话后生效，旧会话不受影响；角色名字同时写入系统提示词。",
        "noFileChosen": "可选择新的头像图片",
        "resetAvatar": "重置头像"
      }
    }
  },
  "en": {
    "config": {
      "persona": {
        "restoreDefault": "Restore Default",
        "preset": {
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
          }
        }
      },
      "desc": {
        "role": "Who the AI and the user each play (names and avatars)",
        "agents": "The agent's operating instructions and safety boundaries",
        "soul": "Agent personality, tone, character",
        "user": "User info and preferences"
      },
      "uploadAvatar": "Upload Avatar",
      "crop": {
        "title": "Crop Avatar"
      },
      "role": {
        "charNote": "Name and avatar changes only take effect in new sessions; existing sessions are not affected. The role names are also written into the system prompt.",
        "noFileChosen": "Select an avatar image file",
        "resetAvatar": "Reset avatar"
      }
    }
  },
  "ja": {
    "config": {
      "persona": {
        "restoreDefault": "デフォルトに戻す",
        "preset": {
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
          }
        }
      },
      "desc": {
        "role": "AI とユーザーがそれぞれ演じる役割（名前とアバター）",
        "agents": "Agent の操作指示と安全境界",
        "soul": "Agent 人格、トーン、性格",
        "user": "ユーザー情報と好み"
      },
      "uploadAvatar": "アバターをアップロード",
      "crop": {
        "title": "アバターをトリミング"
      },
      "role": {
        "charNote": "名前とアバターの変更は新しいセッション作成後にのみ反映され、既存のセッションには影響しません。役割名はシステムプロンプトにも書き込まれます。",
        "noFileChosen": "新しいアバター画像を選択できます",
        "resetAvatar": "アバターをリセット"
      }
    }
  },
  "ko": {
    "config": {
      "persona": {
        "restoreDefault": "기본값 복원",
        "preset": {
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
          }
        }
      },
      "desc": {
        "role": "AI와 사용자가 각각 맡는 역할(이름과 아바타)",
        "agents": "Agent의 운영 지침과 안전 경계",
        "soul": "Agent 인격, 어조, 성격",
        "user": "사용자 정보 및 선호도"
      },
      "uploadAvatar": "아바타 업로드",
      "crop": {
        "title": "아바타 자르기"
      },
      "role": {
        "charNote": "이름과 아바타 변경은 새 세션 생성 후에만 적용되며, 기존 세션에는 영향을 주지 않습니다. 역할 이름은 시스템 프롬프트에도 기록됩니다.",
        "noFileChosen": "새 아바타 이미지를 선택할 수 있습니다",
        "resetAvatar": "아바타 초기화"
      }
    }
  }
}
</i18n>

<style scoped>
/* The strip's own surface must wrap EVERY tab. The theme paints the background
   and the bottom border on the tab ROW, whose box is capped at the scroller's
   width — a row that overflows (four tabs are wider than a narrow sidebar) then
   scrolls its last tabs onto bare panel background, with the underline cut off
   at the row's edge. `max-content` grows the row with its tabs (so added tabs
   stay wrapped) and `min-width: 100%` keeps the underline spanning the panel
   when a tab is removed. */
:deep(.p-tabview-tablist) {
  width: max-content;
  min-width: 100%;
}

/* 让面板内容区成为 flex 列容器：根布局用 flex-1 精确填充内容区高度，
   内部 flex 链（TabView → panels → panel → textarea）自适应伸缩，
   避免 72vh 等固定高度把内容区撑出垂直滚动条。 */
:deep(.p-tabview-panels) {
  display: flex;
  flex-direction: column;
  flex: 1 1 0%;
  min-height: 0;
}

:deep(.p-tabview-panel) {
  display: flex;
  flex: 1 1 0%;
  min-height: 0;
}
</style>
