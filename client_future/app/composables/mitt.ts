import mitt from 'mitt';

const emitter = mitt();
export const emit = emitter.emit;// 触发事件方法 $emit
export const on = emitter.on;// 监听事件方法 $on