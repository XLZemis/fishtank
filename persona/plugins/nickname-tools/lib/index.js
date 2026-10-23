/**
 * persona-nickname-tools —— 她给群友起/改「自定昵称」的工具（用户 2026-10-20）
 *
 * 背景：前几天有人把群名片和头像改成和主人一模一样，骗过她一回。
 * 现在的锚点是：**主人的 QQ 号（1000000002）永远显示为「主人」**，与群名片无关；
 * 其他人可以用这个工具起一个她认得的称呼，绑定 QQ 号，跨群生效。
 *
 * 三个工具：
 *   nickname_list   —— 看已有哪些自定昵称
 *   nickname_set    —— 给某个 QQ 起/改昵称（qq + 昵称）
 *   nickname_remove —— 删掉某个 QQ 的昵称
 *
 * 主人 QQ 在平台侧是黑名单：改名/删除都会返回 403，工具描述里也写死了这一点。
 */

import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';

const BRIDGE = process.env.PERSONA_BRIDGE_URL || 'http://127.0.0.1:13101';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
const OWNER_QQ = process.env.PERSONA_OWNER_QQ || '';

export const name = 'persona-nickname-tools';
export const inject = ['tools'];

function token() {
  try {
    return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch {
    return '';
  }
}

async function call(method, path, body) {
  const t = token();
  if (!t) return { ok: false, error: '读不到桥 token' };
  try {
    const resp = await fetch(`${BRIDGE}${path}`, {
      method,
      headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

const DESCRIPTION = `给群友维护「自定昵称」：绑定 QQ 号，跨群生效，用来称呼他们。
主人（QQ ${OWNER_QQ}）是**黑名单**：平台永远显示为「主人」，这个工具改不了他，也不许改。
用法：nickname_set {qq:"123456789", name:"小猫"}；nickname_list 看现状；nickname_remove {qq:"…"} 删掉。
注意：昵称是你自己的记忆标注，不要起侮辱性称呼；不确定是谁就先 nickname_list 看看。`;

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'nickname_list',
    description: `${DESCRIPTION}\n当前动作：列出全部自定昵称。`,
    parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async () => {
      const r = await call('GET', '/nicknames');
      if (!r.ok) return { text: `读取失败：${r.error}` };
      const map = r.map || {};
      const lines = Object.entries(map).map(([qq, n]) => `  ${n}（${qq}）`);
      return {
        text: `主人的 QQ 固定显示为「主人」（${r.owner}，不可改）。\n` +
          (lines.length ? `已设定的自定昵称 ${lines.length} 个：\n${lines.join('\n')}` : '还没有设定过任何自定昵称。'),
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'nickname_set',
    description: `${DESCRIPTION}\n当前动作：给一个 QQ 设定/覆盖昵称。`,
    parameters: {
      qq: { type: 'string', required: true, description: '群友的 QQ 号（5-12 位数字）' },
      name: { type: 'string', required: true, description: '你想怎么称呼他（≤24 字）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const qq = String(args.qq || '').trim();
      if (qq === OWNER_QQ) return { text: `主人的 QQ（${OWNER_QQ}）已锁定为「主人」，不允许改自定昵称。` };
      const r = await call('POST', '/nicknames', { qq, name: String(args.name || '').trim() });
      if (!r.ok) return { text: `设定失败：${r.error}` };
      return { text: `已记住：${String(args.name).trim()}（${qq}）。下次他说话就会用这个称呼。` };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'nickname_remove',
    description: `${DESCRIPTION}\n当前动作：删掉某个 QQ 的自定昵称。`,
    parameters: {
      qq: { type: 'string', required: true, description: '要删除昵称的 QQ 号' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const qq = String(args.qq || '').trim();
      if (qq === OWNER_QQ) return { text: `主人的昵称不可删除。` };
      const r = await call('DELETE', `/nicknames?qq=${encodeURIComponent(qq)}`);
      if (!r.ok) return { text: `删除失败：${r.error}` };
      return { text: r.removed ? `已删除 ${qq} 的自定昵称。` : `${qq} 本来就没有自定昵称。` };
    },
  }));
}
