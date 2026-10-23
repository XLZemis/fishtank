/**
 * persona-sticker-tools —— 表情图片保存 + 视觉标注
 *
 * 她可以：
 *   sticker_save(source="/persona/out/xxx.jpg")
 *   sticker_save(source="https://.../x.png", tags="看戏, 吐槽")
 *   sticker_save(source="12345")          ← 这批消息里的 media_id
 *
 * 流程：取图 → 校验 → 调用感知层 /stickers/tag（MiMo 视觉打标）→ 写入
 * /persona/stickers/own-uploads/ → 原子更新 manifest.json。只进她自己的专属库，
 * 不碰公共生产库；同一 sha256 不重复存。
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { defineTool } from '@deepseek-ai/dsh-tools';

const BOT = process.env.PERSONA_PERCEPTION_BASE || 'http://qqbot-nonebot:8080';
const BRIDGE = process.env.PERSONA_BRIDGE_URL || 'http://127.0.0.1:13101';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
const STICKER_ROOT = process.env.PERSONA_STICKER_ROOT || '/persona/stickers';
const MANIFEST = path.join(STICKER_ROOT, 'manifest.json');
const UPLOAD_DIR = 'own-uploads';
const MAX_BYTES = 12 * 1024 * 1024;

export const name = 'persona-sticker-tools';
export const inject = ['tools'];

function token() {
  try {
    return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch {
    return '';
  }
}

function sniffExt(buf, fallback = 'jpg') {
  if (!buf || buf.length < 12) return fallback;
  if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return 'jpg';
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return 'png';
  if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46) return 'gif';
  if (buf.slice(0, 4).toString('ascii') === 'RIFF' && buf.slice(8, 12).toString('ascii') === 'WEBP') return 'webp';
  return fallback;
}

function extFromMime(mime) {
  const m = String(mime || '').toLowerCase();
  if (m.includes('png')) return 'png';
  if (m.includes('gif')) return 'gif';
  if (m.includes('webp')) return 'webp';
  return 'jpg';
}

async function fetchSource(source) {
  const text = String(source || '').trim();
  if (!text) throw new Error('source 为空');
  if (/^https?:\/\//i.test(text)) {
    const resp = await fetch(text, { signal: AbortSignal.timeout(25_000) });
    if (!resp.ok) throw new Error(`下载失败 HTTP ${resp.status}`);
    const buf = Buffer.from(await resp.arrayBuffer());
    return { buf, ext: sniffExt(buf, extFromMime(resp.headers.get('content-type'))) };
  }
  if (/^\d+$/.test(text)) {
    const resp = await fetch(`${BRIDGE}/media/file?id=${encodeURIComponent(text)}`, {
      headers: { authorization: `Bearer ${token()}` },
      signal: AbortSignal.timeout(25_000),
    });
    if (!resp.ok) throw new Error(`media ${text} 还没缓存到人格容器；先用 media_look 看它一次`);
    const buf = Buffer.from(await resp.arrayBuffer());
    return { buf, ext: sniffExt(buf, sniffExt(buf, String(resp.headers.get('x-ext') || 'jpg').replace(/^\./, ''))) };
  }
  let p = text;
  if (!p.startsWith('/')) p = path.join('/persona', p);
  if (!p.startsWith('/persona/')) throw new Error('本地路径只允许 /persona/ 下');
  const buf = fs.readFileSync(p);
  return { buf, ext: sniffExt(buf, path.extname(p).replace(/^\./, '') || 'jpg') };
}

async function tagImage(buf, mime) {
  const t = token();
  if (!t) return { ok: false, error: '读不到 token' };
  try {
    const resp = await fetch(`${BOT}/stickers/tag`, {
      method: 'POST',
      headers: { authorization: `Bearer ${t}`, 'content-type': 'application/json' },
      body: JSON.stringify({ image_b64: buf.toString('base64'), mime, tier: 'flash' }),
      signal: AbortSignal.timeout(90_000),
    });
    return await resp.json();
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

let chain = Promise.resolve();
function withManifestLock(fn) {
  const next = chain.then(fn, fn);
  chain = next.catch(() => {});
  return next;
}

function readManifest() {
  return JSON.parse(fs.readFileSync(MANIFEST, 'utf8'));
}

function writeManifest(manifest) {
  manifest.count = (manifest.items || []).length;
  manifest.generated_at = new Date().toISOString();
  const tmp = `${MANIFEST}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmp, JSON.stringify(manifest, null, 1), 'utf8');
  fs.renameSync(tmp, MANIFEST);
}

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'sticker_save',
    description:
      '把一张图存进**你自己的专属表情包库**，并自动做视觉标注（caption/情绪/用途/态度/anti_tags）。\n' +
      'source 可以是：http(s) 图片 URL、这批消息里的 media_id（纯数字）、或 /persona/ 下已有的图片路径。\n' +
      'name 可选（文件名/短名）；tags 可选（逗号分隔，会补进 usage_tags，如 "看戏,吐槽"）。\n' +
      '同一内容（sha256）不会重复存；安全风险 high 的图会被拒绝。存完要过几分钟检索缓存才刷新。',
    parameters: {
      source: { type: 'string', description: '图片 URL / media_id（纯数字）/ /persona/... 路径' },
      name: { type: 'string', description: '可选：短名或文件名（不含路径）' },
      tags: { type: 'string', description: '可选：额外用途标签，逗号/顿号分隔' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { text: { type: 'string' } } },
      render: (_args, value) => [{ type: 'text', text: value.text }],
    },
    execute: async (args) => {
      const source = String(args.source || '').trim();
      if (!source) return { text: 'source 不能为空。' };
      try {
        const { buf, ext } = await fetchSource(source);
        if (!buf || buf.length < 200) return { text: '取到的图片太小，可能不是有效图片。' };
        if (buf.length > MAX_BYTES) return { text: `图片太大（${buf.length} bytes，上限 12MB）。` };
        const sha = crypto.createHash('sha256').update(buf).digest('hex');
        const mime = ext === 'png' ? 'image/png' : ext === 'gif' ? 'image/gif' : ext === 'webp' ? 'image/webp' : 'image/jpeg';
        const tagged = await tagImage(buf, mime);
        const tags = (tagged && tagged.ok && tagged.tags) ? tagged.tags : {};
        if (tags.safety_risk === 'high') {
          return { text: `这张图被判定为 high 风险（${tags.risk_reason || '视觉模型判定'}），不存。` };
        }
        const userTags = String(args.tags || '').split(/[,，、;；\s]+/).map((x) => x.trim()).filter(Boolean).slice(0, 6);
        if (userTags.length) {
          tags.usage_tags = Array.from(new Set([...(tags.usage_tags || []), ...userTags])).slice(0, 6);
        }
        // 在锁里检查重复并写 manifest
        return await withManifestLock(async () => {
          const manifest = readManifest();
          const existing = (manifest.items || []).find((it) => it.sha256 === sha);
          if (existing) {
            return { text: `这张图已经在了：${existing.id}（${existing.file}）\n标签：${JSON.stringify(existing.tags || {})}` };
          }
          const id = `own-${sha.slice(0, 12)}`;
          const rawName = String(args.name || tags.caption || id).replace(/[^\w\-\u4e00-\u9fa5]+/g, '_').slice(0, 40) || id;
          const rel = path.join(UPLOAD_DIR, `${id}.${ext}`);
          const full = path.join(STICKER_ROOT, rel);
          fs.mkdirSync(path.dirname(full), { recursive: true });
          fs.writeFileSync(full, buf);
          const item = {
            id,
            pack: 'own',
            file: rel,
            name: rawName,
            sha256: sha,
            size: buf.length,
            ext,
            animated: ext === 'gif',
            caption: String(tags.caption || rawName).slice(0, 80),
            keywords: Array.isArray(tags.keywords) ? tags.keywords.slice(0, 6).join(',') : '',
            tags: {
              emotions: tags.emotions || [],
              usage_tags: tags.usage_tags || [],
              attitudes: tags.attitudes || [],
              anti_tags: tags.anti_tags || [],
            },
            source: 'agent-save',
            license: 'personal',
            safety_risk: tags.safety_risk || 'low',
            risk_reason: '',
            enabled: true,
            reviewed: true,
            confidence: 0.6,
          };
          manifest.items = [...(manifest.items || []), item];
          manifest.stats = { ...(manifest.stats || {}), own: (Number(manifest.stats?.own) || 0) + 1 };
          try {
            writeManifest(manifest);
          } catch (err) {
            try { fs.unlinkSync(full); } catch {}
            throw err;
          }
          return {
            text: [
              `已存进专属库：${id}`,
              `文件：/persona/stickers/${rel}`,
              `画面：${item.caption}`,
              `标签：情绪=${(item.tags.emotions || []).join('/') || '—'}；用途=${(item.tags.usage_tags || []).join('/') || '—'}；态度=${(item.tags.attitudes || []).join('/') || '—'}`,
              `标注模型：${tagged?.model || '未成功（已用空标签保存）'}`,
            ].join('\n'),
          };
        });
      } catch (err) {
        return { text: `保存失败：${String(err)}` };
      }
    },
  }));
}
