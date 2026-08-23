import mitt from 'mitt';

const emitter = mitt();
export const emit = emitter.emit;// 触发事件方法 $emit
export const on = emitter.on;// 监听事件方法 $on
export const off = emitter.off;// 取消监听方法 $off

// 仅开发环境暴露测试钩子：允许 Playwright 等工具直接向 mitt 总线注入事件（如 ws:notification）。
// 生产构建（import.meta.env.DEV === false）下不注入任何全局变量。
if (import.meta.env.DEV) {
  (window as unknown as { __emitTest: typeof emit }).__emitTest = emit;
}