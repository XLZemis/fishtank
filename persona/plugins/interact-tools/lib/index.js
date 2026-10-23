/**
 * persona-interact-tools —— 戳一戳 / 点赞 / 读取头像
 *
 * poke/like 走感知层 /interact（计划器在 bot 进程里执行，支持延迟 + 间隔 + 次数）；
 * avatar 直接拉 QQ 头像存到 /persona/out，返回值里的路径可直接 read_image。
 */

import fs from 'node:fs';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';

const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
const OWNER_QQ = process.env.PERSONA_OWNER_QQ || '1000000002';
const OUT_DIR = process.env.PERSONA_OUT_DIR || '/persona/out';

export const name = 'persona-interact-tools';
export const inject = ['tools'];

function token() {
  try {
    return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch {
    return '';
  }
}

function resolveTarget(raw) {
  const text = String(raw || '').trim();
  if (text === '主人' || text.toLowerCase() === 'owner') return OWNER_QQ;
  return text;
}

async function post(pathname, body) {
  const t = token();
  if (!t) return { ok: false, error: '读不到 bridge token' };
  try {
    const resp = await fetch(`${BOT}${pathname}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

async function avatar(target) {
  const url = `https://q1.qlogo.cn/g?b=qq&nk=${encodeURIComponent(target)}&s=640`;
  let bytes;
  let ext = 'jpg';
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(15_000) });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const type = String(resp.headers.get('content-type') || '');
    if (type.includes('png')) ext = 'png';
    else if (type.includes('webp')) ext = 'webp';
    else if (type.includes('gif')) ext = 'gif';
    bytes = Buffer.from(await resp.arrayBuffer());
  } catch (err) {
    return { text: `头像 URL：${url}\n（下载失败：${String(err)}；可在浏览器打开这个 URL）` };
  }
  if (!bytes || bytes.length < 300) {
    return { text: `头像 URL：${url}\n（返回内容太小，可能不是有效头像）` };
  }
  try {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const file = path.join(OUT_DIR, `avatar_${target}.${ext}`);
    fs.writeFileSync(file, bytes);
    return {
      text: `头像已保存：/persona/out/avatar_${target}.${ext}\n原图 URL：${url}\n大小：${bytes.length} bytes\n（可用 read_image 看这张图）`,
    };
  } catch (err) {
    return { text: `头像 URL：${url}\n（保存到本地失败：${String(err)}）` };
  }
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'interact',
    description:
      '互动工具：戳一戳(poke)、点赞(like)、读取头像(avatar)。\n' +
      'poke/like 支持“延迟开始 + 每隔 N 秒重复 M 次”，例如 times=5, interval_seconds=10,\n' +
      'start_delay_seconds=30；不传间隔/次数就是立即做一次。\n' +
      'target 是 5–12 位 QQ 号（也可以写“主人”）；scope 省略时自动用当前会话（群戳/私聊戳）。\n' +
      'avatar 会把 QQ 头像拉到 /persona/out/avatar_<QQ>.jpg，可直接用 read_image 看。\n' +
      '限制：times 1–20；计划总跨度最多 1 小时；重复动作最小间隔 1 秒；同时最多 20 个计划。',
    parameters: {
      action: {
        type: 'string',
        enum: ['poke', 'like', 'avatar'],
        description: 'poke=戳一戳，like=点赞，avatar=读取头像',
      },
      target: { type: 'string', description: '对方 QQ 号（5–12 位）；头像也可用“主人”' },
      times: { type: 'number', description: '执行次数，默认 1，上限 20' },
      interval_seconds: { type: 'number', description: '每次之间的秒数，默认 0；重复时最小 1' },
      start_delay_seconds: { type: 'number', description: '第一次执行前延迟多少秒，默认 0' },
      scope: { type: 'string', description: '可选：当前会话 scope；省略时自动使用本轮会话' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const action = String(args.action || '').trim().toLowerCase();
      const target = resolveTarget(args.target);
      if (!/^\d{5,12}$/.test(target)) return { text: 'target 必须是 5–12 位 QQ 号。' };
      if (action === 'avatar') return await avatar(target);
      if (!['poke', 'like'].includes(action)) return { text: 'action 只能是 poke / like / avatar。' };
      const scope = String(args.scope || process.env.PERSONA_DECISION_SCOPE || '').trim();
      const times = Math.max(1, Math.min(Number(args.times) || 1, 20));
      const interval = Math.max(0, Math.min(Number(args.interval_seconds) || 0, 3600));
      const delay = Math.max(0, Math.min(Number(args.start_delay_seconds) || 0, 3600));
      const result = await post('/interact', {
        action, target, scope, times,
        interval_seconds: interval,
        start_delay_seconds: delay,
      });
      if (!result.ok) return { text: `互动失败：${result.error || '未知错误'}` };
      if (result.executed) {
        return { text: `✓ 已经${action === 'poke' ? '戳了' : '点赞'} ${target}` };
      }
      const job = result.job || {};
      return {
        text: [
          `已安排 ${action === 'poke' ? '戳一戳' : '点赞'}：${target} × ${job.times || times}`,
          `· 首次：${job.start_delay_seconds || delay} 秒后`,
          `· 间隔：${job.interval_seconds ?? interval} 秒`,
          `· job：${job.job_id || result.job_id || '?'}（预计 ${Math.round((job.start_delay_seconds || delay) + ((job.times || times) - 1) * (job.interval_seconds ?? interval))} 秒内完成）`,
          result.warning ? `· ${result.warning}` : '',
        ].filter(Boolean).join('\n'),
      };
    },
  }));
}
