import dayjs from 'dayjs';
import type { Dayjs } from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';

/**
 * 会话列表的纯筛选工具（供 SessionSidebar 在本地对已加载的 historyList 做客户端过滤，
 * 不发起任何请求）。关键字（标题）与创建日期范围两个条件同时生效（AND），均可选。
 *
 * `createTime` 存在两种格式（见 SessionSidebar.loadSessionList / handleCreateSession）：
 * - 服务端会话：紧凑 14 位 `YYYYMMDDHHmmss`（如 `20260621004725`）；
 * - 本地占位会话：`YYYY-MM-DD HH:mm`（如 `2026-06-21 00:47`）。
 * 另保留宽松 ISO 兜底；三者都无法解析时返回 null。
 */

// customParseFormat 插件必须在解析前注册才能按自定义格式解析字符串。
// common/utils.ts 在应用侧已注册过一次；dayjs.extend 对同一插件重复调用是幂等的
// （仅重复包装原型方法，行为不变），这里在模块加载时同样注册一次，
// 保证本模块在任何入口（包括单元测试）单独加载时都可用。
dayjs.extend(customParseFormat);

/**
 * 解析会话创建时间为 Dayjs 对象。
 *
 * 依次尝试（前两步严格模式，避免误匹配）：
 * 1. 紧凑 14 位 `YYYYMMDDHHmmss`（服务端会话）；
 * 2. `YYYY-MM-DD HH:mm`（本地占位会话）；
 * 3. 宽松 ISO 兜底（dayjs 默认解析）。
 *
 * @param raw 原始时间字符串
 * @returns 解析成功返回 Dayjs；空值/无法解析返回 null
 */
export function parseSessionCreateTime(raw: string): Dayjs | null {
  if (!raw) return null;
  const compact = dayjs(raw, 'YYYYMMDDHHmmss', true);
  if (compact.isValid()) return compact;
  const local = dayjs(raw, 'YYYY-MM-DD HH:mm', true);
  if (local.isValid()) return local;
  // 纯数字串只允许通过严格 14 位紧凑格式解析：JS Date 的宽松兜底解析会把
  // 非法紧凑串（如 13 月的 `20261321004725`）「进位」成看似合法的日期（2027-01）。
  if (/^\d+$/.test(raw.trim())) return null;
  const fallback = dayjs(raw);
  return fallback.isValid() ? fallback : null;
}

/** 日期范围数组中是否至少存在一个有效日期（区分「未启用日期筛选」与「已启用」） */
function hasRangeValue(range: Date[] | null): boolean {
  return Array.isArray(range) && range.some(d => d != null);
}

/** 当前是否有任一筛选条件生效（关键字非空白，或日期范围含有效日期） */
export function hasActiveSessionFilter(keyword: string, range: Date[] | null): boolean {
  return keyword.trim().length > 0 || hasRangeValue(range);
}

/**
 * 判断单个会话是否同时满足关键字与日期范围筛选（AND）。
 *
 * - 关键字：大小写不敏感的 `includes` 匹配；空白关键字视为未启用；
 * - 日期范围：包含整个起始日与整个结束日（startOf/endOf('day')）；
 *   支持完整 `[start, end]` 与部分 `[start]`（仅有下界）；
 * - 日期筛选启用时，createTime 无法解析的会话被排除；未启用时直接放行。
 *
 * @param item 待判断的会话（至少含 title 与 createTime）
 * @param keyword 标题关键字（原始值，内部自行 trim）
 * @param range 日期范围（PrimeVue Calendar range 模式的值，可为 null）
 */
export function matchesSessionFilter(
  item: { title: string; createTime: string },
  keyword: string,
  range: Date[] | null
): boolean {
  const kw = keyword.trim().toLowerCase();
  if (kw && !item.title.toLowerCase().includes(kw)) return false;

  if (!hasRangeValue(range)) return true;

  const created = parseSessionCreateTime(item.createTime);
  // 日期筛选启用时：无法解析的创建时间无法判定归属，直接排除。
  if (!created) return false;

  const startRaw = Array.isArray(range) ? range[0] : null;
  const endRaw = Array.isArray(range) && range.length > 1 ? (range[1] ?? null) : null;
  // 部分范围 [start]：仅有下界（起始日 00:00:00 起）。
  if (startRaw && created.isBefore(dayjs(startRaw).startOf('day'))) return false;
  // 完整范围 [start, end]：上界取结束日 23:59:59.999，包含整个结束日。
  if (endRaw && created.isAfter(dayjs(endRaw).endOf('day'))) return false;
  return true;
}

/**
 * 过滤会话列表：关键字与日期范围同时生效（AND）。
 *
 * @param list 会话列表
 * @param keyword 标题关键字（原始值，内部自行 trim）
 * @param range 日期范围（可为 null）
 * @returns 两个筛选条件均未启用时原样返回同一数组引用；否则返回过滤后的新数组
 */
export function filterSessions<T extends { title: string; createTime: string }>(
  list: T[],
  keyword: string,
  range: Date[] | null
): T[] {
  if (!hasActiveSessionFilter(keyword, range)) return list;
  return list.filter(item => matchesSessionFilter(item, keyword, range));
}
