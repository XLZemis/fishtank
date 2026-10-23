/**
 * persona-chat-search —— 她的"聊天记录检索"工具
 *
 * 设计（用户 2026-10-18）：
 *   不做摘要/群档案/话题统计这类"机器味功能"；只做**检索**——指定文本、指定群友、指定时间段。
 *   数据来自宿主每 60 秒同步的只读快照 /persona/data/pool/pool.sqlite3（不碰生产库）。
 *
 * 提供 1 个工具：
 *   chat_search —— 按 文本 / 群 / 人 / 时间 / 条数 / 排序 检索历史消息
 *
 * 返回格式刻意保留她熟悉的归一化样子：`[群:名字] MM-DD HH:mm [昵称]：内容`
 * 她可以把结果直接引用、也可以据此回答"上周谁提过 XX"。
 */

import fs from 'node:fs';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';

// 运行时（node:22）自带 node:sqlite 吗？22.5+ 有 experimental；DSH 运行时是 22.x，
// 为了稳，这里用「能拿到就用 node:sqlite，拿不到就调 sqlite3 命令行」的策略。
const POOL = process.env.PERSONA_POOL_DB || '/persona/data/pool/pool.sqlite3';
const UPDATED = '/persona/data/pool/pool.updated';

let DatabaseSync = null;
try {
  // eslint-disable-next-line node/no-unsupported-features/node-builtins
  ({ DatabaseSync } = await import('node:sqlite'));
} catch {
  DatabaseSync = null;
}

/** 把「2h / 3d / 30m / 2026-10-17 08:00 / 10-17」解析成 unix 秒；解析不了返回 null */
function parseTime(input, fallbackMs) {
  if (input === undefined || input === null || input === '') return fallbackMs;
  const s = String(input).trim();
  const m = s.match(/^(\d+)\s*(m|h|d|w)$/i);
  if (m) {
    const n = Number(m[1]);
    const unit = m[2].toLowerCase();
    const sec = { m: 60, h: 3600, d: 86400, w: 604800 }[unit];
    return Date.now() - n * sec * 1000;
  }
  // 纯数字：当成 unix 秒
  if (/^\d{10}$/.test(s)) return Number(s) * 1000;
  // 日期/日期时间（按北京时间解释）
  const mm = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$/) || s.match(/^(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?$/);
  if (mm) {
    const year = mm[1].length === 4 ? Number(mm[1]) : new Date().getFullYear();
    const month = Number(mm[mm[1].length === 4 ? 2 : 1]);
    const day = Number(mm[mm[1].length === 4 ? 3 : 2]);
    const hour = Number(mm[mm[1].length === 4 ? 4 : 3] ?? 0);
    const min = Number(mm[mm[1].length === 4 ? 5 : 4] ?? 0);
    // 北京时间 = UTC+8
    return Date.UTC(year, month - 1, day, hour - 8, min, 0);
  }
  return fallbackMs;
}

function fmtTime(tsMs) {
  // 池库里的 ts 是**秒**（bus 用 int(time.time()) 写入）；小数字说明是秒，转成毫秒再渲染
  const n = Number(tsMs);
  const d = new Date(n < 1e12 ? n * 1000 : n);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 执行查询：返回 [{scope, groupName, nickname, userId, text, ts}] */
function queryPool(opts) {
  const { text, scope, sender, sinceMs, untilMs, limit, order } = opts;
  if (!fs.existsSync(POOL)) {
    const err = new Error(`检索库还没生成（宿主每 60 秒同步一次）：${POOL}`);
    err.code = 'NOPOOL';
    throw err;
  }

  const where = [];
  const args = [];
  if (text) {
    where.push('(text LIKE ? OR COALESCE(nickname,\'\') LIKE ?)');
    args.push(`%${text}%`, `%${text}%`);
  }
  if (scope && scope !== 'all') {
    where.push('scope LIKE ?');
    args.push(scope.includes(':') ? scope : `%${scope}%`);
  }
  if (sender) {
    // QQ 号精确，昵称走模糊
    if (/^\d{5,12}$/.test(String(sender))) {
      where.push('user_id = ?');
      args.push(String(sender));
    } else {
      where.push('COALESCE(nickname,\'\') LIKE ?');
      args.push(`%${sender}%`);
    }
  }
  if (sinceMs) {
    where.push('ts >= ?');
    args.push(Math.floor(sinceMs / 1000));
  }
  if (untilMs) {
    where.push('ts <= ?');
    args.push(Math.floor(untilMs / 1000));
  }
  const sql = `SELECT scope, group_name AS groupName, nickname, user_id AS userId, text, ts
               FROM messages ${where.length ? `WHERE ${where.join(' AND ')}` : ''}
               ORDER BY seq ${order === 'oldest' ? 'ASC' : 'DESC'} LIMIT ?`;
  args.push(limit);

  const db = new DatabaseSync(`file:${POOL}?mode=ro`, { readOnly: true });
  try {
    return { rows: db.prepare(sql).all(...args) };
  } finally {
    db.close();
  }
}

/** 群名清理：池子里有 `<$ÿĀ\u0011\u0010>` 这类协议端噪声 */
function cleanGroup(name) {
  return String(name || '').replace(/<\$[^>]*>/g, '').trim();
}

/** 把一行池记录压成她读得顺的样子（去掉开头的 [群:..] 时间 [昵称]：前缀，只留正文） */
function bodyOf(text, nickname, tsMs) {
  const raw = String(text || '');
  const ts = fmtTime(tsMs);
  // 池子里的行形如：`[群:示例群] 09:28 [某人]：内容`
  const m = raw.match(/^\[[^\]]*\]\s*[^[]*?\[([^\]]+)\]：([\s\S]*)$/);
  if (m) return `${ts} [${m[1]}]：${m[2]}`;
  return `${ts} [${nickname || '?'}]：${raw}`;
}

export const name = 'persona-chat-search';
export const inject = ['tools'];

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'chat_search',
    description:
      '检索历史聊天记录（她记得的、看过的、错过的都能查）。可按 文本 / 群 / 群友 / 时间 / 条数 组合筛选。\n' +
      '数据是宿主每 60 秒同步的只读快照，不是实时的（最新一分钟可能查不到）。\n' +
      '用法示例：\n' +
      '  · 上周谁提过"显卡" → chat_search {query:"显卡", since:"7d"}\n' +
      '  · 主人在示例群2群说过什么 → chat_search {sender:"1000000002", scope:"1000000004", limit:10}\n' +
      '  · 昨天下午示例群聊了什么 → chat_search {scope:"1000000003", since:"1d", limit:20}\n' +
      '  · 谁说过"生日" → chat_search {query:"生日", since:"3d"}\n' +
      '返回 `MM-DD HH:mm [昵称]：内容`，按时间倒序；需要正序传 order:"oldest"。',
    parameters: {
      query: { type: 'string', description: '要搜的文本（子串匹配，也会匹配昵称）。省略 = 不限文本。' },
      scope: { type: 'string', description: '范围：群号（如 1000000004）、"group:1000000004"、或 "all"。省略 = 全部。' },
      sender: { type: 'string', description: '群友：QQ 号（精确）或昵称片段（模糊）。' },
      since: { type: 'string', description: '起始时间：相对 "2h"/"3d"/"1w"，或 "2026-10-17 08:00"、"10-17"。' },
      until: { type: 'string', description: '截止时间，格式同 since；省略 = 现在。' },
      limit: { type: 'number', description: '最多返回几条，默认 20，上限 50。' },
      order: { type: 'string', description: '"newest"（默认，最新在前）或 "oldest"。' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const limit = Math.min(Math.max(Number(args.limit) || 20, 1), 50);
      const sinceMs = args.since ? parseTime(args.since, 0) : 0;
      const untilMs = args.until ? parseTime(args.until, Date.now()) : 0;
      const order = args.order === 'oldest' ? 'oldest' : 'newest';
      let rows;
      try {
        ({ rows } = queryPool({
          text: args.query ? String(args.query) : '',
          scope: args.scope ? String(args.scope) : 'all',
          sender: args.sender ? String(args.sender) : '',
          sinceMs,
          untilMs,
          limit,
          order,
        }));
      } catch (err) {
        return { text: `检索失败：${err.message}` };
      }
      if (!rows.length) {
        const bits = [];
        if (args.query) bits.push(`文本「${args.query}」`);
        if (args.sender) bits.push(`来自 ${args.sender}`);
        if (args.since) bits.push(`自 ${args.since}`);
        return { text: `没有找到${bits.length ? bits.join('、') + ' 的' : ''}记录。` };
      }
      // 按群分组输出，读起来更像翻聊天记录
      const lines = rows.map((r) => {
        const g = cleanGroup(r.groupName) || r.scope;
        return `[${g}] ${bodyOf(r.text, r.nickname, r.ts)}`;
      });
      let updated = '';
      try {
        updated = fs.readFileSync(UPDATED, 'utf8').trim();
      } catch {
        updated = '未知';
      }
      return { text: `共 ${rows.length} 条（检索库更新于 ${updated}）\n` + lines.join('\n') };
    },
  }));
}
