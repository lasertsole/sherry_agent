import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

// 必须先注册该插件，才能解析自定义格式的字符串
dayjs.extend(customParseFormat);

/**
 * 转换紧凑型时间字符串（如 20260621004725）为指定格式
 * @param timeStr 紧凑的时间字符串，通常为 14 位
 * @param format 输出的目标格式，例如 'YYYY-MM-DD HH:mm:ss'
 * @returns 格式化后的时间字符串，若输入无效则返回空字符串
 */
export const formatCompactTimeString = (timeStr: string | number, format: string = 'YYYY-MM-DD HH:ss'): string => {
  if (!timeStr) return '';

  // 统一转换为字符串并去除前后空格
  const str = String(timeStr).trim();

  // 严格校验：如果是标准的 14 位纯数字字符串再进行解析（可根据实际后端返回微调长度）
  if (str.length !== 14 || isNaN(Number(str))) {
    return '';
  }

  // 核心：传入第二个参数 'YYYYMMDDHHmmss'，明确告诉 dayjs 如何拆解这个字符串
  const date = dayjs(str, 'YYYYMMDDHHmmss');

  // 防御：检查解析后的日期是否合法
  return date.isValid() ? date.format(format) : '';
};
