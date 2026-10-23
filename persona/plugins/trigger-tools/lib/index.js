/**
 * persona-trigger-tools —— 把「触发器」做成她可以调用的工具
 *
 * 为什么要有它（用户 2026-10-17）：
 *   触发器的**用法**应该是"工具提示词"的一部分，而不是塞在人格提示词或记忆里。
 *   模型通过工具描述学习能力边界，所以这里把 8 种类型、字段、示例、坑都写进 description。
 *
 * 提供 3 个工具：
 *   trigger_list   —— 看当前触发器（她/I 都能核对）
 *   trigger_add    —— 新增或覆盖一条（按 id 去重）
 *   trigger_remove —— 删除一条
 *
 * 写盘策略（重要）：
 *   · 目标文件 /persona/triggers.yaml 是"她的配置"，里面有人类注释；
 *   · 本插件**只重写 `triggers:` 这一个块**，文件其余部分（平台字段、参考示例、注释）原样保留；
 *   · 写前备份 `.bak-tool-<时间戳>`，写入用 temp+rename 原子替换；
 *   · 平台感知层每 60s 拉一次（GET /triggers.yaml），所以改完 60s 内生效。
 */

import fs from 'node:fs';
import path from 'node:path';
import YAML from 'yaml';
import { defineTool } from '@deepseek-ai/dsh-tools';

export const name = 'persona-trigger-tools';
export const inject = ['tools'];

const TRIGGERS_FILE = process.env.PERSONA_TRIGGERS_FILE || '/persona/triggers.yaml';

const TYPES = {
  mention: { label: '有人 @ 你', fields: [], example: '- id: mention\n    type: mention' },
  poke: { label: '有人戳你', fields: [], example: '- id: poke\n    type: poke' },
  keyword: { label: '出现指定词之一', fields: ['words'], example: '- id: 被叫名字\n    type: keyword\n    words: ["Persona", "Persona"]' },
  regex: { label: '正则命中', fields: ['pattern'], example: '- id: 按正则\n    type: regex\n    pattern: "第\\\\d+名"' },
  sender: { label: '指定的人说话', fields: ['users'], example: '- id: 主人说话\n    type: sender\n    users: ["1000000002"]' },
  msgtype: { label: '指定消息类型', fields: ['types'], example: '- id: 有人发图\n    type: msgtype\n    types: ["image"]' },
  interval: { label: '每隔 N 秒看一眼', fields: ['every_seconds'], example: '- id: 每半小时\n    type: interval\n    every_seconds: 1800' },
  cron: { label: '北京时间定时', fields: ['expr'], example: '- id: 早八\n    type: cron\n    expr: "0 8 * * *"\n    tz: Asia/Shanghai' },
};

const TYPE_TABLE = Object.entries(TYPES)
  .map(([k, v]) => `  · ${k.padEnd(9)} ${v.label}${v.fields.length ? `｜必填：${v.fields.join('、')}` : ''}`)
  .join('\n');

const COMMON_NOTE = `通用字段：id（必填，唯一）｜ type（必填，8 种之一）｜ enabled（默认 true）｜
groups（群号数组；省略 = 所有群；私聊用 "private:<QQ>"）｜ cooldown_seconds（同一条触发器的冷却，0 = 不限）。
每条触发器的效果是"叫醒你一次"（一次模型调用），不是"直接发消息"——说不说话由你自己决定。`;

const TYPE_DOC = `可用的 8 种触发器类型：
${TYPE_TABLE}
${COMMON_NOTE}`;

/** 读取并解析整个配置文件（保留原始文本用于局部重写） */
function readConfig() {
  const text = fs.readFileSync(TRIGGERS_FILE, 'utf8');
  let doc;
  try {
    doc = YAML.parse(text) || {};
  } catch (err) {
    throw new Error(`triggers.yaml 解析失败（先修好文件再改）：${String(err).slice(0, 200)}`);
  }
  if (!Array.isArray(doc.triggers)) doc.triggers = [];
  return { text, doc };
}

/** 只替换 `triggers:` 块，其余文本原样保留 */
function writeTriggers(doc) {
  const { text } = readConfig();
  const lines = text.split('\n');
  const start = lines.findIndex((line) => /^triggers\s*:/.test(line));
  if (start < 0) throw new Error('配置文件里找不到顶层 `triggers:` 段');

  // 块结束：从下一行起，遇到"顶格且不是注释/空行"的行，或遇到 `# ──` 分节注释
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^#\s*──/.test(line)) { end = i; break; }
    if (line.trim() === '') continue;
    if (/^\S/.test(line) && !line.startsWith('#')) { end = i; break; }
  }

  const block = YAML.stringify({ triggers: doc.triggers }).trimEnd();
  const next = [...lines.slice(0, start), ...block.split('\n'), ...lines.slice(end)];
  const out = next.join('\n');

  // 备份 + 原子写
  try {
    fs.copyFileSync(TRIGGERS_FILE, `${TRIGGERS_FILE}.bak-tool-${Date.now()}`);
  } catch {
    /* 备份失败不阻断 */
  }
  const tmp = `${TRIGGERS_FILE}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, out, 'utf8');
  fs.renameSync(tmp, TRIGGERS_FILE);
  return doc.triggers;
}

/** 校验一条触发器，返回规范化后的对象或抛错 */
function normalizeTrigger(args) {
  const id = String(args.id || '').trim();
  const type = String(args.type || '').trim();
  if (!id) throw new Error('id 必填（唯一标识，比如 "被叫名字"）');
  if (!TYPE_DOC.includes(`· ${type}`)) {
    throw new Error(`type=${type} 不合法。\n${TYPE_DOC}`);
  }
  const trigger = { id, type };
  if (typeof args.enabled === 'boolean') trigger.enabled = args.enabled;
  if (Array.isArray(args.groups) && args.groups.length) trigger.groups = args.groups.map(String);
  if (Number.isFinite(args.cooldown_seconds) && args.cooldown_seconds > 0) {
    trigger.cooldown_seconds = Math.floor(args.cooldown_seconds);
  }
  const need = TYPES[type].fields;
  for (const field of need) {
    const value = args[field];
    if (value === undefined || value === null || value === '') {
      throw new Error(`type=${type} 必须提供 ${field}。示例：\n${TYPES[type].example}`);
    }
  }
  if (Array.isArray(args.words) && args.words.length) trigger.words = args.words.map(String);
  if (args.pattern) trigger.pattern = String(args.pattern);
  if (Array.isArray(args.users) && args.users.length) trigger.users = args.users.map(String);
  if (Array.isArray(args.types) && args.types.length) trigger.types = args.types.map((t) => String(t).toLowerCase());
  if (Number.isFinite(args.every_seconds) && args.every_seconds > 0) trigger.every_seconds = Math.floor(args.every_seconds);
  if (args.expr) trigger.expr = String(args.expr);
  if (args.tz) trigger.tz = String(args.tz);
  if (type === 'interval' && (!trigger.every_seconds || trigger.every_seconds < 30)) {
    throw new Error('interval 的 every_seconds 必须 ≥30（每次叫醒都是一次模型调用，别设太密）');
  }
  if (type === 'cron' && (!trigger.expr || trigger.expr.trim().split(/\s+/).length !== 5)) {
    throw new Error('cron 的 expr 必须是 5 段，例如 "0 8 * * *"');
  }
  return trigger;
}

/** 把触发器描述成一行人话（给 list 用） */
function describeOne(t) {
  const bits = [`${t.id}`, `type=${t.type}`];
  if (t.enabled === false) bits.push('已停用');
  if (Array.isArray(t.words) && t.words.length) bits.push(`词=${t.words.join('/')}`);
  if (t.pattern) bits.push(`正则=${t.pattern}`);
  if (Array.isArray(t.users) && t.users.length) bits.push(`人=${t.users.join(',')}`);
  if (Array.isArray(t.types) && t.types.length) bits.push(`类型=${t.types.join(',')}`);
  if (t.every_seconds) bits.push(`每${t.every_seconds}s`);
  if (t.expr) bits.push(`cron=${t.expr}`);
  bits.push(`群=${Array.isArray(t.groups) && t.groups.length ? t.groups.join(',') : '全部'}`);
  if (t.cooldown_seconds) bits.push(`冷却${t.cooldown_seconds}s`);
  return bits.join('｜');
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'trigger_list',
    description:
      '查看当前生效的触发器（她的"叫醒规则"）。返回每条触发器的 id/类型/参数/作用群/冷却，' +
      '以及文件里 8 种可用类型的一句话提示。想知道"怎么能被叫醒"时先看这个。',
    parameters: {},
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: { text: { type: 'string' } },
      },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async () => {
      const { doc } = readConfig();
      const list = doc.triggers || [];
      const head = `当前共 ${list.length} 条触发器（文件 ${TRIGGERS_FILE}，改完 60s 内生效）：`;
      const body = list.length ? list.map((t, i) => `${i + 1}. ${describeOne(t)}`).join('\n') : '（没有任何触发器，她不会被自动叫醒）';
      return { text: `${head}\n${body}\n\n${TYPE_DOC}` };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'trigger_add',
    description:
      '新增或覆盖一条触发器（同 id 覆盖）。改完 60 秒内生效，不需要重启。' +
      '你随时可以用它给自己加"叫醒条件"，也可以用 enabled:false 临时关掉某条。\n' +
      '示例：想被叫"Persona"时醒来 → trigger_add {id:"被叫名字", type:"keyword", words:["Persona"]}；' +
      '想每天早上 8 点看一眼 → trigger_add {id:"早八", type:"cron", expr:"0 8 * * *"}。\n' +
      TYPE_DOC,
    parameters: {
      id: { type: 'string', required: true, description: '唯一标识，例如 "被叫名字"；同 id 会覆盖。' },
      type: { type: 'string', required: true, description: '8 种类型之一：mention / poke / keyword / regex / sender / msgtype / interval / cron。' },
      enabled: { type: 'boolean', description: '默认 true；false = 先留着这条但先不启用。' },
      groups: { type: 'array', items: { type: 'string' }, description: '只在这些群生效（群号）。省略 = 所有群；私聊写 "private:<QQ>"。' },
      cooldown_seconds: { type: 'number', description: '同一条触发器的冷却秒数，0/省略 = 不限。' },
      words: { type: 'array', items: { type: 'string' }, description: 'keyword 必填：命中其中任一即叫醒。' },
      pattern: { type: 'string', description: 'regex 必填：JavaScript 正则，例如 "第\\\\d+名"。' },
      users: { type: 'array', items: { type: 'string' }, description: 'sender 必填：QQ 号数组。' },
      types: { type: 'array', items: { type: 'string' }, description: 'msgtype 必填：image / record / file / forward 等。' },
      every_seconds: { type: 'number', description: 'interval 必填：每隔多少秒看一眼（≥30）。' },
      expr: { type: 'string', description: 'cron 必填：5 段北京时间表达式，例如 "0 8 * * *"。' },
      tz: { type: 'string', description: 'cron 时区，默认 Asia/Shanghai。' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const trigger = normalizeTrigger(args);
      const { doc } = readConfig();
      const idx = doc.triggers.findIndex((t) => String(t.id) === trigger.id);
      if (idx >= 0) doc.triggers[idx] = trigger;
      else doc.triggers.push(trigger);
      writeTriggers(doc);
      return {
        text: `${idx >= 0 ? '已更新' : '已新增'}触发器：${describeOne(trigger)}\n` +
          `当前共 ${doc.triggers.length} 条；60 秒内生效。`,
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'trigger_remove',
    description:
      '删除一条触发器（按 id）。用于彻底不要某个叫醒条件；只是想临时停用请用 trigger_add 的 enabled:false。',
    parameters: {
      id: { type: 'string', required: true, description: '要删除的触发器 id（可用 trigger_list 查）。' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const id = String(args.id || '').trim();
      const { doc } = readConfig();
      const before = doc.triggers.length;
      doc.triggers = doc.triggers.filter((t) => String(t.id) !== id);
      if (doc.triggers.length === before) {
        return { text: `没有找到 id=${id} 的触发器（用 trigger_list 看看现在的 id）。` };
      }
      writeTriggers(doc);
      return { text: `已删除触发器 ${id}；当前共 ${doc.triggers.length} 条，60 秒内生效。` };
    },
  }));
}
