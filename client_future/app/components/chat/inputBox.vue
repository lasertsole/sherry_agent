<template>
     <div
        class="root"
        tabindex="-1"
    >
        <div
            class="inputBox"
            contenteditable="true"
            :readonly="readonly"
            @input.stop="inputFunc($event)"
        >
        </div>

        <Button label="发送" class="send" />
    </div>
</template>

<script lang="ts" setup>
import { type ShallowRef } from 'vue';
import { cloneDeep, isEmpty, isNil } from 'lodash-es';

const chatInput:Ref<string> = ref("")

const inputDom: ShallowRef<HTMLElement | null> = useTemplateRef('inputDom');

const defaultHint = '请输入内容...';
const modelValue = ref(defaultHint);

// 输入回调函数
function inputFunc(event: Event): void {
  if (!(event instanceof InputEvent)) {
    return;
  }

  const target = event.target as HTMLInputElement;

  if (event.inputType === 'deleteContentBackward' && (target.innerHTML === '<br>' || isEmpty(target.innerHTML))) {
    target.textContent = '';
    modelValue.value = '';
  } else if (isEmpty(target.innerHTML)) {
    target.textContent = '';
    modelValue.value = '';
  } else {
    modelValue.value = target.innerHTML;
  }
}
</script>

<style lang="scss" scoped>
    @use "sass:math";
    @use "@/common.scss" as common;

    .root{
        height: 100%;
        width: 100%;
        position: relative;

        >.inputBox{
            height: 100%;
            width: 100%;
            outline: none;
            word-break: break-all;
            padding: 0.5rem;
        }

        >.send{
            position: absolute;
            right: 0.5rem;
            bottom: 0.5rem;
        }
    }
</style>