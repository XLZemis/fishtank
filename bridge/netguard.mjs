/**
 * 桥 · 出站地址策略与受限下载（netguard.mjs）
 *
 * 为什么需要它（2026-10-25 安全复核）：
 *   /media/fetch 原来对任意 URL 直接 `fetch(src, {redirect:'follow'})` 并 `arrayBuffer()`
 *   全量读入内存后才判断大小。这既给了内网探测/SSRF 的入口，也可能被大响应打满堆内存。
 *
 * 本模块只做两件事：
 *   1. hostAllowed：主机名 → 公网判定（拒绝回环/私网/链路本地/组播/保留/元数据，DNS 解析后复判）；
 *   2. fetchGuarded：手动跟随重定向（最多 N 跳，每跳复判），流式读取并累计字节，
 *      超过上限立即 cancel；可选 content-type 白名单 + 媒体文件魔数兜底。
 *
 * 约定：失败抛 Error，err.code ∈ {blocked, limit, upstream}，调用方据此给 403/413/502。
 */
import dns from 'node:dns/promises';
import net from 'node:net';

// ── IPv4 黑名单（左闭右闭区间，用整数比较）──────────────────────────────────
const V4_BLOCKED = [
  [0x00000000, 0x00ffffff], // 0.0.0.0/8
  [0x0a000000, 0x0affffff], // 10/8
  [0x64400000, 0x647fffff], // 100.64/10 CGNAT
  [0x7f000000, 0x7fffffff], // 127/8
  [0xa9fe0000, 0xa9feffff], // 169.254/16 链路本地 / 云元数据
  [0xac100000, 0xac1fffff], // 172.16/12
  [0xc0000000, 0xc00000ff], // 192.0.0/24
  [0xc0000200, 0xc00002ff], // 192.0.2/24
  [0xc0586300, 0xc05863ff], // 192.88.99/24
  [0xc0a80000, 0xc0a8ffff], // 192.168/16
  [0xc6120000, 0xc613ffff], // 198.18/15
  [0xc6336400, 0xc63364ff], // 198.51.100/24
  [0xcb007100, 0xcb0071ff], // 203.0.113/24
  [0xe0000000, 0xefffffff], // 224/4 组播
  [0xf0000000, 0xffffffff], // 240/4 保留
];

const BLOCKED_HOSTS = new Set([
  'localhost',
  'localhost.localdomain',
  'ip6-localhost',
  'ip6-loopback',
  'metadata',
  'metadata.google.internal',
  'metadata.tencentyun.com',
  'instance-data',
]);

const MAX_URL_CHARS = 4096;
const REDIRECT_CODES = new Set([301, 302, 303, 307, 308]);
const CACHE_TTL_MS = 10 * 60 * 1000;
const resolveCache = new Map();

/** 构造带 code 的错误（便于调用方映射 HTTP 状态码） */
function fail(code, message) {
  const err = new Error(message);
  err.code = code;
  return err;
}

function ipv4ToInt(ip) {
  const parts = String(ip).split('.');
  if (parts.length !== 4) return null;
  let value = 0;
  for (const part of parts) {
    // 拒绝 "01" 这种非规范写法：Number('01')=1，但不同解析器行为不一，直接当非法
    if (!/^(0|[1-9]\d{0,2})$/.test(part)) return null;
    const n = Number(part);
    if (n > 255) return null;
    value = ((value << 8) >>> 0) + n;
  }
  return value >>> 0;
}

/** True = 这个 IP 不允许出站（IPv4/IPv6 都处理） */
export function isBlockedIp(value) {
  const ip = String(value || '').trim();
  if (!ip) return true;
  if (net.isIPv4(ip)) {
    const n = ipv4ToInt(ip);
    if (n === null) return true;
    return V4_BLOCKED.some(([lo, hi]) => n >= lo && n <= hi);
  }
  if (!net.isIPv6(ip)) return true; // 既不是 v4 也不是 v6 → fail-closed
  let s = ip.toLowerCase();
  if (s.startsWith('::ffff:')) {
    const tail = s.slice(7);
    if (net.isIPv4(tail)) return isBlockedIp(tail);
  }
  if (s === '::' || s === '::1') return true;
  const first = parseInt(s.split(':')[0] || '0', 16);
  if (Number.isFinite(first)) {
    if ((first & 0xfe00) === 0xfc00) return true; // fc00::/7 ULA
    if ((first & 0xffc0) === 0xfe80) return true; // fe80::/10 链路本地
    if ((first & 0xff00) === 0xff00) return true; // ff00::/8 组播
  }
  if (s.startsWith('64:ff9b:') || s.startsWith('2001:db8:') || s.startsWith('100::')) return true;
  return false;
}

/** 解析主机名（带 10 分钟缓存）；失败返回空数组 */
async function resolveHost(host) {
  const key = host.toLowerCase().replace(/\.$/, '');
  const now = Date.now();
  const hit = resolveCache.get(key);
  if (hit && hit.exp > now) return hit.ips;
  let rows;
  try {
    rows = await dns.lookup(key, { all: true, verbatim: true });
  } catch {
    return [];
  }
  const ips = rows.map((r) => r.address).filter(Boolean);
  if (ips.length) resolveCache.set(key, { exp: now + CACHE_TTL_MS, ips });
  return ips;
}

/** 主机名白名单匹配：pattern 本身或其子域 */
function inAllowList(host, allowHosts) {
  return (allowHosts || []).some((pattern) => {
    const p = String(pattern || '').toLowerCase().replace(/\.$/, '');
    return p && (host === p || host.endsWith(`.${p}`));
  });
}

/**
 * 判断主机名是否允许出站。
 * @returns {Promise<{allowed:boolean, reason:string, ip?:string}>}
 */
export async function hostAllowed(hostname, { allowHosts = [] } = {}) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '');
  if (!host) return { allowed: false, reason: '缺少主机名' };
  if (BLOCKED_HOSTS.has(host) || host.endsWith('.localhost')) {
    return { allowed: false, reason: `主机名被拒绝：${host}` };
  }
  if (inAllowList(host, allowHosts)) return { allowed: true, reason: '白名单主机', ip: host };
  if (net.isIP(host)) {
    return isBlockedIp(host)
      ? { allowed: false, reason: `地址不在公网范围：${host}` }
      : { allowed: true, reason: '公网 IP', ip: host };
  }
  const ips = await resolveHost(host);
  if (!ips.length) return { allowed: false, reason: `域名解析失败：${host}` };
  for (const ip of ips) {
    if (isBlockedIp(ip)) return { allowed: false, reason: `域名解析到受限地址：${host} -> ${ip}` };
  }
  return { allowed: true, reason: '公网域名', ip: ips[0] };
}

/** 魔数兜底：content-type 缺失/不透传时，至少确认是媒体文件 */
export function looksLikeMedia(buf) {
  const b = Buffer.isBuffer(buf) ? buf : Buffer.from(buf || []);
  if (b.length < 12) return false;
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return true; // JPEG
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) return true; // PNG
  if (b.slice(0, 3).toString('ascii') === 'GIF') return true; // GIF
  if (b.slice(0, 4).toString('ascii') === 'RIFF' && b.slice(8, 12).toString('ascii') === 'WEBP') return true;
  if (b.slice(4, 8).toString('ascii') === 'ftyp') return true; // MP4/MOV
  if (b.slice(0, 4).toString('ascii') === 'OggS') return true;
  if (b.slice(0, 3).toString('ascii') === 'ID3') return true; // MP3
  if (b[0] === 0xff && (b[1] & 0xe0) === 0xe0) return true; // MP3 帧头
  if (b.slice(0, 5).toString('ascii') === '#!AMR') return true;
  if (b.slice(0, 4).toString('ascii') === 'RIFF' && b.slice(8, 12).toString('ascii') === 'WAVE') return true;
  return false;
}

/** 流式读取，累计字节超过 maxBytes 立刻 cancel 并抛 limit 错误 */
async function readCapped(stream, maxBytes) {
  const reader = stream.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > maxBytes) {
      try {
        await reader.cancel();
      } catch {
        // cancel 失败不重要，下面的错误才是结论
      }
      throw fail('limit', `响应超过上限 ${maxBytes} 字节`);
    }
    chunks.push(Buffer.from(value));
  }
  return Buffer.concat(chunks);
}

/**
 * 受限下载：手动跟随重定向、每跳做地址策略、流式限量。
 *
 * @param {string} src 目标 URL
 * @param {object} opts
 * @param {number} [opts.maxBytes=30MB]
 * @param {number} [opts.maxHops=3]
 * @param {string[]} [opts.allowHosts=[]]
 * @param {string[]} [opts.allowContentTypes=[]] 允许的 content-type 前缀；空 = 不校验
 * @param {number} [opts.timeoutMs=20000]
 * @returns {Promise<{buf:Buffer, contentType:string, finalUrl:string, bytes:number}>}
 */
export async function fetchGuarded(src, {
  maxBytes = 30 * 1024 * 1024,
  maxHops = 3,
  allowHosts = [],
  allowContentTypes = [],
  timeoutMs = 20000,
} = {}) {
  let current = String(src || '').trim();
  if (!current) throw fail('blocked', 'url 为空');
  if (current.length > MAX_URL_CHARS) throw fail('blocked', 'url 过长');

  for (let hop = 0; hop <= maxHops; hop += 1) {
    let parsed;
    try {
      parsed = new URL(current);
    } catch {
      throw fail('blocked', 'url 解析失败');
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      throw fail('blocked', `协议不允许：${parsed.protocol}`);
    }
    if (parsed.username || parsed.password) throw fail('blocked', 'url 不允许携带用户信息');
    const gate = await hostAllowed(parsed.hostname, { allowHosts });
    if (!gate.allowed) throw fail('blocked', gate.reason);

    let resp;
    try {
      resp = await fetch(current, { redirect: 'manual', signal: AbortSignal.timeout(timeoutMs) });
    } catch (err) {
      throw fail('upstream', `请求失败：${String(err && err.message ? err.message : err)}`);
    }
    if (REDIRECT_CODES.has(resp.status)) {
      const location = resp.headers.get('location');
      if (!location) throw fail('upstream', '重定向缺少 location');
      current = new URL(location, current).toString();
      continue;
    }
    if (!resp.ok) throw fail('upstream', `upstream ${resp.status}`);
    const contentType = String(resp.headers.get('content-type') || '').toLowerCase();
    const declared = Number(resp.headers.get('content-length') || 0);
    if (declared && declared > maxBytes) throw fail('limit', `响应超过上限 ${maxBytes} 字节`);
    const buf = await readCapped(resp.body, maxBytes);
    if (!buf.length) throw fail('upstream', 'empty body');
    if (allowContentTypes.length) {
      const typeOk = allowContentTypes.some((t) => contentType.startsWith(String(t).toLowerCase()));
      if (!typeOk && !looksLikeMedia(buf)) {
        throw fail('blocked', `content-type 不允许：${contentType || '(空)'}`);
      }
    }
    return { buf, contentType, finalUrl: current, bytes: buf.length };
  }
  throw fail('blocked', '重定向次数过多');
}

/** 测试/配置变更后清缓存 */
export function clearCache() {
  resolveCache.clear();
}
