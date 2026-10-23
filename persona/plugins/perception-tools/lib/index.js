/**
 * persona-perception-tools —— 让她能"翻聊天记录"和"看自己的运行数据"
 *
 * 用户 2026-10-20 第 6、7 项：
 *   6) 详细格式：拉取前 N 条消息，带 QQ / 自定昵称 / 时间 / 群名 / 正文
 *   7) 运行数据：决策次数、token 输入输出、缓存命中、出站数（原 bot 的 /ops status）
 *
 * 数据在感知层（qqbot 容器）的库和 HTTP 接口里，本插件只做转发：
 *   GET http://qqbot-nonebot:8080/messages?scope=&limit=&before_seq=
 *   GET http://qqbot-nonebot:8080/stats?hours=
 *
 * 鉴权用 /persona/bridge-token（与决策桥同一把 token）。
 */

import fs from 'node:fs';
import { defineTool } from '@deepseek-ai/dsh-tools';

const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';

export const name = 'persona-perception-tools';
export const inject = ['tools'];

function token() {
  try {
    return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch {
    return '';
  }
}

async function post(path, body) {
  const t = token();
  if (!t) return { ok: false, error: '读不到 token' };
  try {
    const resp = await fetch(`${BOT}${path}`, {
      method: 'POST',
      headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

async function get(path) {
  const t = token();
  if (!t) return { ok: false, error: '读不到 token' };
  try {
    const resp = await fetch(`${BOT}${path}`, { headers: { authorization: `Bearer ${t}` } });
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

const DETAIL_DESC = `拉取聊天记录的**详细格式**：每条带 [QQ号][昵称][群名][时间] 和正文，比平时看到的更全。
用在你需要确认"这句话到底是谁说的、什么时候说的"的时候（比如有人冒充主人）。
参数：scope 可以是群名片段（如 "示例群2"）、"group:1000000004"、"private:1000000002" 或 "all"；
limit 最多 50 条；before_seq 从某条往前翻（默认从最新开始）。`;

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'chat_detail',
    description: DETAIL_DESC,
    parameters: {
      scope: { type: 'string', description: '群名片段 / group:<群号> / private:<QQ> / all；省略=全部' },
      limit: { type: 'number', description: '条数，默认 20，上限 50' },
      before_seq: { type: 'number', description: '从这条 seq 之前往前翻（用于继续翻页）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const scope = encodeURIComponent(String(args.scope || 'all'));
      const limit = Math.min(Math.max(Number(args.limit) || 20, 1), 50);
      const before = Number(args.before_seq) ? `&before_seq=${Number(args.before_seq)}` : '';
      const r = await get(`/messages?scope=${scope}&limit=${limit}${before}`);
      if (!r.ok) return { text: `拉取失败：${r.error}` };
      if (!r.count) return { text: '这个范围里没有消息。' };
      const lines = r.messages.map(
        (m) => `[${m.qq}][${m.nickname || '?'}][${m.group || (m.scope.startsWith('private:') ? '私聊' : m.scope)}][${m.time}] ${m.text}`,
      );
      const oldest = r.messages[r.messages.length - 1];
      return {
        text: `共 ${r.count} 条（最旧 seq=${oldest.seq}，想继续翻就传 before_seq=${oldest.seq}）：\n` + lines.join('\n'),
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'perception_stats',
    description:
      '看运行数据（像原来的 /ops status）：最近 N 小时的决策次数、说话/沉默比、输入输出 token、' +
      '缓存命中率、平均耗时、出站条数。想知道自己这两天花了多少、缓存有没有生效时用。',
    parameters: {
      hours: { type: 'number', description: '统计窗口小时数，默认 24' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const hours = Math.min(Math.max(Number(args.hours) || 24, 1), 720);
      const r = await get(`/stats?hours=${hours}`);
      if (!r.ok) return { text: `读取失败：${r.error}` };
      return {
        text: [
          `最近 ${r.hours} 小时：`,
          `· 决策 ${r.decisions} 次（说话 ${r.speak} / 沉默 ${r.silence}）`,
          `· 输入 ${r.input_tokens} tokens，输出 ${r.output_tokens} tokens`,
          `· 缓存读 ${r.cache_read_tokens}，命中率 ${r.cache_hit_rate}%`,
          `· 平均耗时 ${r.avg_elapsed_ms} ms`,
          `· 出站 ${r.sends} 条（成功 ${r.sends_ok}）`,
        ].join('\n'),
      };
    },
  }));

  ctx.tools.register(defineTool({
    name: 'media_look',
    description:
      '真正"看/听"一个图片、音频或视频（不只是看文件名）。三档：\n' +
      '  · tier="asr"   → 只把语音转成文字（视频还会抽帧，便宜）\n' +
      '  · tier="flash" → MiMo v2.6 Flash 原生理解（默认，性价比最高）\n' +
      '  · tier="pro"   → MiMo v2.6 Pro 原生理解（最细，贵一些）\n' +
      '用 media_id 指这批里的媒体（如 1024），或用 url 指公网地址；\n' +
      'frames/fps 控制视频抽帧数量与采样率，prompt 可以指定"你想让它描述什么"。',
    parameters: {
      media_id: { type: 'number', description: '媒体编号（这批消息里出现的 #数字）' },
      url: { type: 'string', description: '或者直接给 http(s) 地址 / 本地路径' },
      tier: { type: 'string', description: 'asr | flash | pro，默认 flash' },
      frames: { type: 'number', description: '视频抽帧数量（asr 档/降级时用），默认 4' },
      fps: { type: 'number', description: '视频原生理解时的采样帧率，默认 1' },
      prompt: { type: 'string', description: '你想让它关注什么，例如"视频里有几个人"、"这段音频在说什么"' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const body = {
        media_id: Number(args.media_id) || undefined,
        url: String(args.url || '') || undefined,
        tier: String(args.tier || 'flash'),
        frames: Number(args.frames) || 4,
        fps: Number(args.fps) || 1,
        prompt: String(args.prompt || ''),
      };
      const t = token();
      if (!t) return { text: '读不到 token' };
      let r;
      try {
        const resp = await fetch(`${BOT}/media/understand`, {
          method: 'POST',
          headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' },
          body: JSON.stringify(body),
        });
        r = await resp.json();
      } catch (err) {
        return { text: `调用失败：${String(err)}` };
      }
      if (!r.ok) return { text: `理解失败：${r.error}` };
      if (r.asr) {
        const t2 = (r.asr && r.asr.ok && r.asr.text) || (r.asr && r.asr.error) || '（没转写出文字）';
        return { text: `语音转写：${t2}${r.frames ? `\n（另外抽了 ${r.frames} 帧画面，想看细节可以用 url 再调 flash 档）` : ''}` };
      }
      return { text: `（${r.model || r.tier}/${r.mode || 'native'}，${r.elapsed_ms}ms）\n${r.text || ''}` };
    },
  }));

  // ── B 站视频（PC 代理优先，服务器浏览器兜底）────────────────────────────
  ctx.tools.register(defineTool({
    name: 'bili_info',
    description:
      '解析一个 B 站资源，返回结构化摘要：\n' +
      '  · 视频（BV/av 号）：标题、UP、时长、播放/弹幕/评论、简介、分P；\n' +
      '  · 专栏（read/cv…）：标题、作者、阅读/点赞/收藏、正文摘要；\n' +
      '  · 动态（t.bilibili.com/… 或 /opus/…）：作者、时间、正文、赞/评/转发；\n' +
      '  · 个人主页（space.bilibili.com/…）：昵称、等级、签名、粉丝/关注/投稿数；\n' +
      '  · 番剧/直播：标题与页面简介。\n' +
      '参数 target 给 BV 号或任意 B 站链接（b23.tv 短链也行）。\n' +
      '什么时候用：群里有人发 B 站卡片/链接、或有人说“看看这个视频/专栏/主页”时；\n' +
      '平台的【这批里的 B 站链接】区块已经自动解析过标题，需要更多细节再调。',
    parameters: {
      target: { type: 'string', required: true, description: 'BV 号、av 号或任意 B 站链接' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const target = String(args.target || '').trim();
      if (!target) return { text: '需要 BV 号或 B 站链接。' };
      const r = await post('/bili/info', { target });
      if (!r.ok) return { text: `读取失败：${r.error || '未知错误'}` };
      return { text: r.text || JSON.stringify(r.data || {}).slice(0, 800) };
    },
  }));

  // ── B 站互动 / 动态 / 消息（登录态已就绪）──────────────────────────────
  ctx.tools.register(defineTool({
    name: 'bili',
    description:
      'B 站互动、发动态、看消息（登录态已就绪）。action：\n' +
      '  · like / unlike —— 给视频点赞/取消赞（target=BV 号或 B 站视频链接）\n' +
      '  · favorite / unfavorite —— 收藏/取消收藏到默认收藏夹\n' +
      '  · coin —— 投币（不可撤销）。**两段式**：先不传 confirm 调一次，会返回当前硬币余额和计划枚数；\n' +
      '    你确认后再传 confirm=true、count=1或2 调第二次才真正投。\n' +
      '  · comment —— 给视频发评论（target + text；公开可见）\n' +
      '  · post_dynamic —— 发一条 B 站动态（text；公开可见，纯文本）\n' +
      '  · watch —— 真正看/听视频（复用 media_ai 三档：tier=asr 只转写音频最便宜；\n' +
      '    tier=flash 原生看画面+声音（默认推荐）；tier=pro 最细最贵）。长视频会截断到 max_seconds。\n' +
      '  · danmaku —— 读视频弹幕（target + limit，按时间排序）\n' +
      '  · comments —— 读视频评论（target + limit，按热度，带点赞数与楼中楼）\n' +
      '  · messages —— 看消息中心（messages_type=reply/at/like/system），回复与 @ 会带跳转链接\n' +
      '注意：点赞/收藏可撤回；评论和动态发出去别人能看到；投币不可撤销，必须确认余额和枚数。',
    parameters: {
      action: { type: 'string', required: true, description: 'like/unlike/favorite/unfavorite/coin/comment/post_dynamic/watch/danmaku/comments/messages' },
      target: { type: 'string', description: 'BV 号或 B 站视频链接（like/favorite/coin/comment 用）' },
      text: { type: 'string', description: '评论内容或动态正文' },
      count: { type: 'number', description: '投币枚数，1 或 2，默认 1' },
      confirm: { type: 'boolean', description: '投币第二步必须传 true（第一步不要传）' },
      messages_type: { type: 'string', description: 'reply（回复我的）/at（@我）/like（收到的赞）/system，默认 reply' },
      limit: { type: 'number', description: '条数：弹幕默认 100（上限 500）；评论默认 20（上限 100）；消息默认 10' },
      tier: { type: 'string', description: 'watch 档位：asr（只转写音频，便宜）/ flash（默认，画面+声音）/ pro（最细，贵）' },
      frames: { type: 'number', description: 'watch 抽帧数（兜底用），默认 4' },
      fps: { type: 'number', description: 'watch 原生视频采样帧率，默认 1' },
      prompt: { type: 'string', description: 'watch 时你想关注什么' },
      max_seconds: { type: 'number', description: 'watch 最多看多少秒，默认 180（上限 300）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const body = {
        action: String(args.action || '').trim(),
        target: String(args.target || '').trim(),
        text: String(args.text || ''),
        count: Number(args.count) || 1,
        confirm: Boolean(args.confirm),
        messages_type: String(args.messages_type || 'reply'),
        limit: Number(args.limit) || 10,
        tier: String(args.tier || 'flash'),
        frames: Number(args.frames) || 4,
        fps: Number(args.fps) || 1,
        prompt: String(args.prompt || ''),
        max_seconds: Number(args.max_seconds) || 180,
      };
      if (!body.action) return { text: '要指定 action。' };
      const r = await post('/bili/action', body);
      if (!r.ok) return { text: r.text || `操作失败：${r.error || '未知错误'}` };
      return { text: r.text || JSON.stringify(r.data || {}).slice(0, 800) };
    },
  }));

  // ── QZone 工具（浏览器通道，登录态已保存）──────────────────────────────
  const qzoneTool = (name, desc, action, textParam) => defineTool({
    name,
    description: desc,
    parameters: {
      ...(textParam ? { text: { type: 'string', required: true, description: '要发布/评论的文字' } } : {}),
      limit: { type: 'number', description: '读说说时返回几条，默认 5' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const r = await post('/browser/qzone', {
        action,
        text: String(args.text || ''),
        limit: Number(args.limit) || 5,
      });
      if (!r.ok) return { text: `操作失败：${r.error || '未知错误'}` };
      if (action === 'read') {
        const items = (r.items || []).filter((x) => x && x.trim());
        return { text: items.length ? `空间里现在有 ${items.length} 条：\n` + items.map((x, i) => `${i + 1}. ${x.slice(0, 200)}`).join('\n') : '空间里还没有说说。' };
      }
      if (action === 'post') return { text: r.submitted ? '说说发出去了。' : '发表没有确认成功，可能需要人工看一下页面。' };
      if (action === 'like') return { text: r.liked ? '给最新那条点过赞了。' : '没点上，页面结构可能变了。' };
      if (action === 'comment') return { text: r.commented ? '评论发出去了。' : '评论没成功，页面结构可能变了。' };
      return { text: JSON.stringify(r).slice(0, 300) };
    },
  });

  ctx.tools.register(qzoneTool(
    'qzone_read',
    '读自己的 QQ 空间说说（浏览器通道，登录态持久化）。返回最近的说说文本。',
    'read', false));
  ctx.tools.register(qzoneTool(
    'qzone_post',
    '发一条 QQ 空间说说（公开可见，发布前想清楚）。必须传 text。',
    'post', true));
  ctx.tools.register(qzoneTool(
    'qzone_like',
    '给空间里最新那条说说点赞。',
    'like', false));
  ctx.tools.register(qzoneTool(
    'qzone_comment',
    '给空间里最新那条说说写评论。必须传 text。',
    'comment', true));

  ctx.tools.register(defineTool({
    name: 'web_open',
    description:
      '用服务器上的真实浏览器打开一个网页并返回正文（服务器在墙外，B站/YouTube 等都能读）。' +
      '适用于：群里别人发的链接你想看看内容、查资料、确认某个页面到底写了什么。',
    parameters: {
      url: { type: 'string', required: true, description: 'http(s) 网址' },
      wait_ms: { type: 'number', description: '页面等待毫秒数，默认 4000' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_a, v) => [{ type: 'text', text: v.text }],
    },
    execute: async (args) => {
      const r = await post('/browser/open', { url: String(args.url), wait_ms: Number(args.wait_ms) || 4000 });
      if (!r.ok) return { text: `打开失败：${r.error}` };
      return { text: `《${r.title}》${r.url}\n${(r.text || '').slice(0, 3000)}` };
    },
  }));
}
