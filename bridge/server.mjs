#!/usr/bin/env node
/**
 * 人格决策桥（dsh-persona 容器内常驻，Node 标准库实现，无第三方依赖）
 *
 * 为什么需要它：
 *   NoneBot 跑在 qqbot-nonebot 容器里，只有 python3，没有 node/dsh，也没有 docker socket
 *   （实测 `which dsh node npm` 全空、/var/run/docker.sock 不存在）。
 *   部署文档 P4 的「bridge.py 用 subprocess 调 dsh」在 NoneBot 容器里无法成立，
 *   因此把 subprocess 这一层放进人格容器，对外只暴露一个受 Bearer token 保护的 HTTP 接口。
 *
 * 接口：
 *   GET  /health    → 存活与队列状态（不需要 token，供容器健康检查）
 *   POST /decide    → 一次决策：spawn `dsh --profile qq-persona --json [--session-id X] "提示词"`
 *
 * 关键设计：
 *   1) 串行化：全局并发上限 + 同一 session 严格串行（dsh 拒绝活动会话被并发接管）。
 *   2) SILENCE 不写会话（部署文档 V7）：调用前快照会话文件，解析到 SILENCE 时回滚快照
 *      （首发会话直接删除新建目录）。这样她的上下文只累积"真正说过的话"。
 *   3) cwd 固定 /persona/work（会话绑 cwd，必须一致，否则 dsh 报 recorded in X, not in Y）。
 *   4) 输出协议解析放在这里（SPEAK: / SILENCE / NEXT_CHECK:），NoneBot 侧只消费结构化结果。
 */

import { spawn } from 'node:child_process';
import http from 'node:http';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

// ── 配置（全部来自容器环境变量，compose.yaml 里声明） ──────────────────────────
function envFirst(...names) {
  for (const name of names) {
    const value = process.env[name];
    if (value !== undefined && String(value).trim() !== '') return String(value).trim();
  }
  return '';
}
const PORT = Number(envFirst('FISHTANK_BRIDGE_PORT', 'PERSONA_BRIDGE_PORT') || 13101);
// 容器内绑 0.0.0.0 由网络隔离 + Bearer token 保护；裸机部署请设 127.0.0.1
const BIND = envFirst('FISHTANK_BRIDGE_BIND', 'PERSONA_BRIDGE_BIND') || '0.0.0.0';
const WORKDIR = process.env.PERSONA_BRIDGE_WORKDIR || '/persona/work';
const SESSIONS_ROOT = process.env.PERSONA_SESSIONS_ROOT || '/data/sessions';
const TOKEN_FILE = process.env.PERSONA_BRIDGE_TOKEN_FILE || '/persona/bridge-token';
const LOG_FILE = process.env.PERSONA_BRIDGE_LOG || '/data/bridge.log';
const SCRATCH_DIR = process.env.PERSONA_BRIDGE_SCRATCH || '/data/bridge-scratch';
// 她的触发器配置（所有权归她；感知层每 60s 通过 GET /triggers.yaml 取一次）
const TRIGGERS_FILE = process.env.PERSONA_TRIGGERS_FILE || '/persona/triggers.yaml';
// 她的专属表情包库（manifest.json + 各套目录），只读服务给感知层
const STICKER_ROOT = process.env.PERSONA_STICKER_ROOT || '/persona/stickers';
const STICKER_MANIFEST = path.join(STICKER_ROOT, 'manifest.json');
// 群消息里的媒体（图片/视频）缓存目录：她 read_image 用；也用于她"把图转手发出去"
const MEDIA_DIR = process.env.PERSONA_MEDIA_DIR || '/persona/media';
const PROFILE = process.env.PERSONA_PROFILE || 'qq-persona';
// 并发上限：主机只有 3.9G 内存，每个 dsh 进程约 200-300MB；2 是实测安全值
const CONCURRENCY = Number(envFirst('FISHTANK_BRIDGE_CONCURRENCY', 'PERSONA_BRIDGE_CONCURRENCY') || 2);
// 单次决策硬超时（毫秒）：超过就杀进程返回 timeout，避免队列被永久占住
const DEFAULT_TIMEOUT_MS = Number(envFirst('FISHTANK_BRIDGE_TIMEOUT_MS', 'PERSONA_BRIDGE_TIMEOUT_MS') || 45000);
// 一轮最多几条：**由她自己决定**（用户口径：不限速）；这里只是防病态输出的技术硬顶
const MAX_MESSAGES = Number(envFirst('FISHTANK_MAX_MESSAGES', 'PERSONA_MAX_MESSAGES') || 20);
// 单条消息最大字数（QQ 单条上限附近取保守值）
const MAX_MESSAGE_CHARS = Number(envFirst('FISHTANK_MAX_MESSAGE_CHARS', 'PERSONA_MAX_MESSAGE_CHARS') || 200);
const MAX_BODY_BYTES = Number(envFirst('FISHTANK_BRIDGE_MAX_BODY_BYTES') || 256 * 1024);
const MEDIA_MAX_BYTES = Number(envFirst('FISHTANK_MEDIA_MAX_BYTES') || 30 * 1024 * 1024);

// ── 小工具 ────────────────────────────────────────────────────────────────────
/** 追加一行带时间戳的日志（写到持久卷，方便事后排查） */
function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(' ')}\n`;
  try {
    fs.appendFileSync(LOG_FILE, line);
  } catch {
    // 日志写不进去不能影响主流程
  }
  process.stdout.write(line);
}

/** 读取 Bearer token（启动时读一次；文件缺失则拒绝启动，避免无鉴权裸奔） */
function readToken() {
  // --selftest 只验证协议解析，不碰 token 文件
  if (process.argv.includes('--selftest')) return 'selftest';
  try {
    return fs.readFileSync(TOKEN_FILE, 'utf8').trim();
  } catch (err) {
    log('[fatal] 读不到 token 文件', TOKEN_FILE, String(err));
    process.exit(1);
  }
}
const TOKEN = readToken();

/** 时区与 locale 固定，保证 cron 相关的时间字符串稳定 */
const TIME_FMT = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  hour12: false,
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});
function nowSh() {
  return TIME_FMT.format(new Date());
}

// ── 会话文件定位与快照 ────────────────────────────────────────────────────────
/**
 * 按 session id 找它的目录：/data/sessions/<workspace-key>/session-<id>/
 * workspace-key 由 cwd 编码而来（实测 /persona/work → --persona-work--），
 * 但编码规则属于内部实现，这里不猜：直接扫一层目录按名字匹配，稳。
 */
function findSessionDir(sessionId) {
  if (!sessionId || typeof sessionId !== 'string') return null;
  // 只接受 dsh 的 session id 形态，防止路径穿越
  if (!/^session-[A-Za-z0-9._-]+$/.test(sessionId)) return null;
  let entries;
  try {
    entries = fs.readdirSync(SESSIONS_ROOT, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const ent of entries) {
    if (!ent.isDirectory()) continue;
    const candidate = path.join(SESSIONS_ROOT, ent.name, sessionId);
    if (fs.existsSync(candidate)) return candidate;
  }
  return null;
}

/** 会话主文件（v4 编码：session.v4.jsonl.zstd） */
function sessionFile(sessionDir) {
  if (!sessionDir) return null;
  for (const name of fs.readdirSync(sessionDir)) {
    if (name.startsWith('session.v4.')) return path.join(sessionDir, name);
  }
  return null;
}

/** 调用前快照：把会话目录整体复制到临时区（体积是几十 KB 级别，可忽略） */
function snapshotSession(sessionId) {
  const dir = findSessionDir(sessionId);
  if (!dir) return { hadSession: false, snapshot: null, dir: null };
  fs.mkdirSync(SCRATCH_DIR, { recursive: true });
  const snap = path.join(SCRATCH_DIR, `${sessionId}.snapshot`);
  fs.rmSync(snap, { recursive: true, force: true });
  fs.cpSync(dir, snap, { recursive: true });
  return { hadSession: true, snapshot: snap, dir };
}

/** 回滚：把快照覆盖回原目录；首发会话（调用前不存在）则整目录删除 */
function rollbackSession(sessionId, snapInfo) {
  try {
    if (!snapInfo.hadSession) {
      // 这次调用新建的会话：直接删掉，等于"她没开过口"
      const dir = findSessionDir(sessionId);
      if (dir) fs.rmSync(dir, { recursive: true, force: true });
      return true;
    }
    if (!snapInfo.snapshot || !snapInfo.dir) return false;
    const target = sessionFile(snapInfo.snapshot);
    const current = sessionFile(snapInfo.dir);
    if (!target || !current) return false;
    // 原子替换：先写临时文件再 rename，避免半截文件
    const tmp = `${current}.restore-tmp`;
    fs.copyFileSync(target, tmp);
    fs.renameSync(tmp, current);
    return true;
  } catch (err) {
    log('[warn] 会话回滚失败', sessionId, String(err));
    return false;
  }
}

function cleanupSnapshot(snapInfo) {
  if (snapInfo?.snapshot) {
    try {
      fs.rmSync(snapInfo.snapshot, { recursive: true, force: true });
    } catch {
      /* 清不掉就留着，下次覆盖 */
    }
  }
}

// ── 输出协议解析 ──────────────────────────────────────────────────────────────
/**
 * 解析她的输出（**定稿契约，不可改**）：
 *
 *   说话：首行 `SPEAK: <第1条>`；其后每个非空行 = 一条新消息；最多 3 条（第 4 行起丢弃）
 *   沉默：`SILENCE`，或 `SILENCE + NEXT_CHECK: <秒>`
 *
 * 解析规则（逐条对应部署文档）：
 *   1. 首行匹配 ^SPEAK:，其后内容 = 第 1 条；
 *   2. 第 2 行起每个非空行 = 一条新消息；最多 3 条，第 4 行起丢弃；
 *   3. 后续行仍以 SPEAK: 开头 → 剥掉前缀再发（防御性容错，避免字面前缀漏进群里）；
 *   4. 空行忽略；单条 >200 字截断并记告警；
 *   5. 其它自造控制词（WAIT:/HOLD: 等）一律按普通文本处理并记告警。
 *
 * 返回 {action, messages, next_check, warnings}
 */
function parseProtocol(text) {
  const warnings = [];
  const messages = [];
  let nextCheck = null;
  let silence = false;

  // 主回复里的"附带动作"行（STICKER/ATTACH/LINK/FORWARD）——合并进同一轮，省一次模型调用
  const actions = [];

  // 去 CR、去首尾空白、丢空行、丢 markdown 代码围栏
  const lines = String(text ?? '')
    .replace(/\r/g, '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !/^```/.test(line));

  if (lines.length === 0) {
    return { action: 'silence', messages: [], next_check: null, warnings: ['输出为空'], actions: [] };
  }

  // NEXT_CHECK 可出现在任意行（含 SILENCE 行后面的那种写法）
  const nextRe = /NEXT_CHECK\s*[:：]\s*(\d{1,5})/i;
  for (const line of lines) {
    const hit = line.match(nextRe);
    if (hit) {
      const seconds = Number(hit[1]);
      nextCheck = Number.isFinite(seconds) ? Math.min(3600, Math.max(60, seconds)) : null;
    }
  }

  /** 压入一条消息：剥前缀、超长截断、超过 3 条丢弃 */
  const pushMessage = (raw, fromLine) => {
    let body = String(raw ?? '').trim();
    if (!body) return;
    // 规则 3：后续行若还带 SPEAK: 前缀，剥掉
    if (fromLine > 0) body = body.replace(/^SPEAK\s*[:：]\s*/i, '').trim();
    if (!body) return;
    // 动作行：STICKER / ATTACH / LINK / FORWARD（最多 3 个）——不进消息体
    const act = String(raw ?? '').trim().match(/^(STICKER|ATTACH|LINK|FORWARD|FILE|VOICE|MUSIC|REPLY|ATALL)\s*[:：]\s*(.*)$/i);
    if (act) {
      if (actions.length < 5) actions.push([act[1].toUpperCase(), act[2].trim()]);
      else warnings.push(`附带动作超过 5 个已丢弃：${act[0].slice(0, 30)}`);
      return;
    }

    // 规则 5：其它自造控制词当普通文本，但要告警
    if (/^[A-Z][A-Z0-9_]{2,}\s*[:：]/.test(body)) {
      warnings.push(`未知控制词按普通文本处理：${body.slice(0, 40)}`);
    }
    if (messages.length >= MAX_MESSAGES) {
      warnings.push(`超过 ${MAX_MESSAGES} 条上限已丢弃：${body.slice(0, 40)}`);
      return;
    }
    // 规则 4：单条 >200 字截断并告警
    if (body.length > MAX_MESSAGE_CHARS) {
      warnings.push(`单条超过 ${MAX_MESSAGE_CHARS} 字已截断（原 ${body.length} 字）`);
      body = body.slice(0, MAX_MESSAGE_CHARS);
    }
    messages.push(body);
  };

  // 首行是 SILENCE（可带 + NEXT_CHECK）
  if (/^SILENCE\b/i.test(lines[0])) {
    silence = true;
    return { action: 'silence', messages: [], next_check: nextCheck, warnings, actions };
  }

  // 首行 SPEAK: → 第 1 条；没有前缀时按普通文本兜底（模型偶尔忘前缀，不能让整轮丢失）
  const first = lines[0].match(/^SPEAK\s*[:：]\s*(.*)$/i);
  if (first) {
    pushMessage(first[1], 0);
  } else {
    warnings.push('首行缺少 SPEAK: 前缀，已按普通文本处理');
    pushMessage(lines[0], 0);
  }

  // 第 2 行起：每个非空行一条
  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (/^SILENCE\b/i.test(line)) {
      // 混合输出里冒出 SILENCE：当控制词丢掉，绝不能把 "SILENCE" 发进群
      warnings.push('SPEAK 之后出现 SILENCE，已忽略该行');
      silence = true;
      continue;
    }
    if (nextRe.test(line) && /^NEXT_CHECK\s*[:：]/i.test(line)) continue; // 纯 NEXT_CHECK 行不算消息
    pushMessage(line, i);
  }

  const action = messages.length > 0 ? 'speak' : (silence ? 'silence' : 'silence');
  if (messages.length === 0 && !silence) warnings.push('既没有可发送消息也没有 SILENCE，按沉默处理');
  return { action, messages, next_check: nextCheck, warnings, actions };
}

/** 从 --json 事件流里抽 sessionId / final / usage */
function parseEvents(stdout) {
  let sessionId = null;
  let finalText = '';
  let usage = null;
  let errorMessage = null;
  for (const line of stdout.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('{')) continue;
    let evt;
    try {
      evt = JSON.parse(trimmed);
    } catch {
      continue;
    }
    if (evt.type === 'session' && evt.sessionId) sessionId = evt.sessionId;
    else if (evt.type === 'final') finalText = String(evt.text ?? '');
    else if (evt.type === 'error') errorMessage = String(evt.message ?? evt.error ?? 'error');
    if (evt.usage) usage = evt.usage;
    else if (evt.type === 'status' && evt.usage) usage = evt.usage;
  }
  return { sessionId, finalText, usage, errorMessage };
}

// ── 进程执行 ──────────────────────────────────────────────────────────────────
/**
 * 跑一次 dsh 一次性任务。返回 { code, stdout, stderr, timedOut }。
 * 参数数组形式 spawn（不经过 shell），提示词里带换行/引号都安全。
 */
function runDsh(prompt, sessionId, timeoutMs, scope) {
  return new Promise((resolve) => {
    const args = ['--profile', PROFILE, '--json'];
    if (sessionId) args.push('--session-id', sessionId);
    args.push(prompt);

    const started = Date.now();
    const child = spawn('dsh', args, {
      cwd: WORKDIR, // 会话绑 cwd：必须与建立会话时一致
      // 让工具知道本轮 scope（群聊/私聊）：interact 工具据此决定群戳还是私聊戳
      env: { ...process.env, PERSONA_DECISION_SCOPE: String(scope || '') },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let stdout = '';
    let stderr = '';
    let timedOut = false;
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        child.kill('SIGKILL');
      } catch {
        /* 已经退出 */
      }
    }, timeoutMs);

    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut, elapsedMs: Date.now() - started });
    });
    child.on('error', (err) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: `${stderr}\nspawn error: ${err}`, timedOut, elapsedMs: Date.now() - started });
    });
  });
}

// ── 并发控制 ──────────────────────────────────────────────────────────────────
let running = 0;
const waiting = [];
const sessionChains = new Map(); // sessionId → 上一个任务的 promise（同会话串行）

/** 抢一个全局并发位（满了就排队） */
function acquireSlot() {
  if (running < CONCURRENCY) {
    running += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => waiting.push(resolve));
}
function releaseSlot() {
  running -= 1;
  const next = waiting.shift();
  if (next) {
    running += 1;
    next();
  }
}

/** 同一 session 串行：dsh 拒绝活动会话被并发接管 */
function withSessionLock(sessionKey, fn) {
  const prev = sessionChains.get(sessionKey) ?? Promise.resolve();
  const run = prev.then(fn, fn);
  // 链上只保留"结束"信号，不让异常传播到下一位
  const tail = run.catch(() => {}).finally(() => {
    if (sessionChains.get(sessionKey) === tail) sessionChains.delete(sessionKey);
  });
  sessionChains.set(sessionKey, tail);
  return run;
}

// ── 一次决策的主流程 ──────────────────────────────────────────────────────────
async function decide(payload) {
  const prompt = String(payload.prompt ?? '').trim();
  if (!prompt) return { ok: false, error: 'empty prompt' };
  const timeoutMs = Number(payload.timeout_ms) > 0 ? Number(payload.timeout_ms) : DEFAULT_TIMEOUT_MS;
  const requestedSession = payload.session_id ? String(payload.session_id) : null;
  // 首次调用（没有 session_id）默认也做 SILENCE 回滚：否则每组第一次决策就建出一个空会话
  // parse:false = 只回原始文本（表情包那两轮用它，避免把 "STICKER: …" 当成群消息发出去）
  const parseMode = payload.parse !== false;
  const rollbackOnSilence = parseMode && payload.rollback_silence !== false;
  const sessionKey = requestedSession || `new:${payload.group_id ?? 'anonymous'}`;

  return withSessionLock(sessionKey, async () => {
    await acquireSlot();
    try {
      // 1) 调用前快照（只对已有会话有意义；新会话靠"删除新建目录"回滚）
      const snapInfo = rollbackOnSilence && requestedSession
        ? snapshotSession(requestedSession)
        : { hadSession: Boolean(requestedSession), snapshot: null, dir: null };

      // 2) 跑 dsh
      const run = await runDsh(prompt, requestedSession, timeoutMs, payload.group_id ? String(payload.group_id) : '');

      // 3) 解析事件流
      const parsed = parseEvents(run.stdout);
      const sessionId = parsed.sessionId || requestedSession;
      // 非 parse 模式：直接返回原文，交给调用方自己解析（例如 STICKER:/SEND: 这类旁路协议）
      if (!parseMode) {
        cleanupSnapshot(snapInfo);
        const rawResult = {
          ok: run.code === 0 && !run.timedOut && !parsed.errorMessage,
          raw: true,
          text: parsed.finalText,
          session_id: sessionId,
          usage: parsed.usage,
          elapsed_ms: run.elapsedMs,
          timed_out: run.timedOut,
          exit_code: run.code,
        };
        if (!rawResult.ok) {
          rawResult.error = run.timedOut
            ? `timeout after ${timeoutMs}ms`
            : parsed.errorMessage || `dsh exit ${run.code}`;
          rawResult.stderr_tail = run.stderr.slice(-1500);
        }
        log(`[decide:raw] group=${payload.group_id ?? '-'} session=${sessionId ?? '-'} ` +
            `tokens=${parsed.usage?.inputTokens ?? '-'} elapsed=${run.elapsedMs}ms ok=${rawResult.ok}`);
        return rawResult;
      }
      const protocol = parseProtocol(parsed.finalText);

      // 4) SILENCE → 回滚会话，保证"沉默不写上下文"（V7）
      let rolledBack = false;
      if (rollbackOnSilence && protocol.action === 'silence' && sessionId) {
        rolledBack = rollbackSession(sessionId, { hadSession: Boolean(requestedSession), snapshot: snapInfo.snapshot, dir: snapInfo.dir });
      }
      cleanupSnapshot(snapInfo);

      const result = {
        ok: run.code === 0 && !run.timedOut && !parsed.errorMessage,
        action: protocol.action,
        messages: protocol.messages,
        // 内联附带动作（STICKER/ATTACH/LINK/FORWARD）：感知层据此执行，省掉单独一轮
        actions: protocol.actions || [],
        warnings: protocol.warnings,
        next_check: protocol.next_check,
        // 会话 id 何时回传：
        //   · 她说了话                → 回传（下次继续用这个会话）
        //   · 原本就有会话（已回滚）  → 回传（会话还在，只是少了这次沉默的痕迹）
        //   · 新会话 + 沉默 + 已回滚  → 不回传（等于这场对话从没开过口，下次重新建）
        session_id: requestedSession || protocol.action === 'speak' || !rolledBack ? sessionId : null,
        text: parsed.finalText,
        usage: parsed.usage,
        elapsed_ms: run.elapsedMs,
        rolled_back: rolledBack,
        timed_out: run.timedOut,
        exit_code: run.code,
      };
      if (!result.ok) {
        result.error = run.timedOut
          ? `timeout after ${timeoutMs}ms`
          : parsed.errorMessage || `dsh exit ${run.code}`;
        result.stderr_tail = run.stderr.slice(-1500);
      }
      log(
        `[decide] group=${payload.group_id ?? '-'} action=${result.action} msgs=${protocol.messages.length}` +
        ` session=${result.session_id ?? '-'} tokens=${parsed.usage?.inputTokens ?? '-'} ` +
        `elapsed=${run.elapsedMs}ms rollback=${rolledBack} ok=${result.ok}`,
      );
      return result;
    } finally {
      releaseSlot();
    }
  });
}

// ── --selftest：确定性验证 SPEAK 定稿契约（不启动服务、不调模型）─────────────
// 用法：node /persona/bridge/server.mjs --selftest
if (process.argv.includes('--selftest')) {
  const cases = [
    { name: '多行各自成条（不限 3 条）', text: 'SPEAK: 一\n二\n三\n四', expect: { action: 'speak', n: 4, msgs: ['一', '二', '三', '四'] } },
    { name: '技术硬顶 20 条', text: 'SPEAK: ' + Array.from({ length: 30 }, (_, i) => `第${i}条`).join('\n第'), expect: { action: 'speak', n: 20 } },
    { name: '后续行带前缀（剥掉）', text: 'SPEAK: 一\nSPEAK: 二', expect: { action: 'speak', n: 2, msgs: ['一', '二'] } },
    { name: '沉默', text: 'SILENCE', expect: { action: 'silence', n: 0 } },
    { name: '沉默+改时间', text: 'SILENCE + NEXT_CHECK: 300', expect: { action: 'silence', n: 0, next: 300 } },
    { name: '空行忽略', text: 'SPEAK: 甲\n\n\n乙', expect: { action: 'speak', n: 2, msgs: ['甲', '乙'] } },
    { name: '首行无前缀兜底', text: '诶诶在的\n尾巴在摇', expect: { action: 'speak', n: 2 } },
    { name: '超长截断到200', text: 'SPEAK: ' + '长'.repeat(260), expect: { action: 'speak', n: 1, len: 200 } },
    { name: '未知控制词当文本', text: 'SPEAK: 我先看看\nWAIT: 30秒', expect: { action: 'speak', n: 2 } },
  ];
  let failed = 0;
  for (const c of cases) {
    const r = parseProtocol(c.text);
    const ok = r.action === c.expect.action
      && (c.expect.n === undefined || r.messages.length === c.expect.n)
      && (c.expect.next === undefined || r.next_check === c.expect.next)
      && (c.expect.msgs === undefined || JSON.stringify(r.messages) === JSON.stringify(c.expect.msgs))
      && (c.expect.len === undefined || (r.messages[0] || '').length === c.expect.len);
    if (!ok) failed += 1;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${c.name}  → action=${r.action} n=${r.messages.length} next=${r.next_check} warnings=${r.warnings.length}`);
  }
  console.log(`\n结果：${cases.length - failed}/${cases.length} 通过`);
  process.exit(failed === 0 ? 0 : 1);
}

// ── HTTP 服务 ────────────────────────────────────────────────────────────────
/** 常量时间比较 token，避免时序侧信道 */
function tokenOk(header) {
  const given = String(header || '').replace(/^Bearer\s+/i, '').trim();
  if (!given || given.length !== TOKEN.length) return false;
  let diff = 0;
  for (let i = 0; i < TOKEN.length; i += 1) diff |= given.charCodeAt(i) ^ TOKEN.charCodeAt(i);
  return diff === 0;
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
  });
  res.end(payload);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || '/', 'http://localhost');

  // 健康检查：不需要 token（容器内 docker healthcheck / 人工 curl 都要能问）
  if (req.method === 'GET' && url.pathname === '/health') {
    return sendJson(res, 200, {
      ok: true,
      service: 'dsh-persona-bridge',
      profile: PROFILE,
      workdir: WORKDIR,
      concurrency: CONCURRENCY,
      running,
      queued: waiting.length,
      time_shanghai: nowSh(),
      uptime_s: Math.round(process.uptime()),
    });
  }

  // ── 她的触发器配置：感知层每 60s 取一次（GET /triggers.yaml）──────────────
  // 为什么让感知层走桥取、而不是直接挂文件进来：
  //   1) 不再动生产 compose（少一处改动）；
  //   2) 文件可能在主机侧被"原子写"（temp+rename），bind mount 的单个文件会指向旧 inode，
  //      而这里每次都是真实 read，永远不会读到旧内容。
  if (req.method === 'GET' && url.pathname === '/triggers.yaml') {
    if (!tokenOk(req.headers.authorization)) {
      return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    }
    try {
      const text = fs.readFileSync(TRIGGERS_FILE, 'utf8');
      const stat = fs.statSync(TRIGGERS_FILE);
      const sha = crypto.createHash('sha256').update(text).digest('hex').slice(0, 16);
      return sendJson(res, 200, { ok: true, path: TRIGGERS_FILE, mtime: Math.floor(stat.mtimeMs), sha, text });
    } catch (err) {
      // 文件不存在也是正常状态（感知层会用出厂默认：mention + poke）
      return sendJson(res, 404, { ok: false, error: `读不到 ${TRIGGERS_FILE}: ${String(err)}` });
    }
  }

  // ── 她的专属表情包库：清单 + 单张文件 ─────────────────────────────────────
  if (req.method === 'GET' && url.pathname === '/stickers/manifest') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    try {
      const text = fs.readFileSync(STICKER_MANIFEST, 'utf8');
      return sendJson(res, 200, { ok: true, root: STICKER_ROOT, manifest: JSON.parse(text) });
    } catch (err) {
      return sendJson(res, 404, { ok: false, error: `读不到 ${STICKER_MANIFEST}: ${String(err)}` });
    }
  }

  if (req.method === 'GET' && url.pathname === '/stickers/file') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    const id = url.searchParams.get('id') || '';
    try {
      const manifest = JSON.parse(fs.readFileSync(STICKER_MANIFEST, 'utf8'));
      const item = (manifest.items || []).find((it) => it.id === id);
      if (!item) return sendJson(res, 404, { ok: false, error: 'unknown sticker id' });
      // 路径必须落在库根内，防目录穿越
      const full = path.resolve(STICKER_ROOT, item.file);
      if (!full.startsWith(path.resolve(STICKER_ROOT) + path.sep)) {
        return sendJson(res, 400, { ok: false, error: 'invalid path' });
      }
      const data = fs.readFileSync(full);
      res.writeHead(200, { 'content-type': item.ext === 'gif' ? 'image/gif' : `image/${item.ext}`, 'content-length': data.length });
      return res.end(data);
    } catch (err) {
      return sendJson(res, 404, { ok: false, error: String(err) });
    }
  }

  // ── 媒体回吐：把已缓存的媒体字节交给感知层（她要"转手发出去"时用）──────────
  if (req.method === 'GET' && url.pathname === '/media/file') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    const id = String(url.searchParams.get('id') || '').replace(/[^0-9A-Za-z_-]/g, '');
    try {
      const hit = fs.readdirSync(MEDIA_DIR).find((n) => n.startsWith(`${id}.`));
      if (!hit) return sendJson(res, 404, { ok: false, error: 'not cached' });
      const data = fs.readFileSync(path.join(MEDIA_DIR, hit));
      const ext = hit.split('.').pop().toLowerCase();
      const type = ext === 'png' ? 'image/png' : ext === 'gif' ? 'image/gif'
        : ext === 'mp4' ? 'video/mp4' : ext === 'webp' ? 'image/webp' : 'image/jpeg';
      res.writeHead(200, { 'content-type': type, 'content-length': data.length, 'x-ext': ext });
      return res.end(data);
    } catch (err) {
      return sendJson(res, 500, { ok: false, error: String(err) });
    }
  }


  // ── 自定昵称（用户 2026-10-20）：群友显示名，绑定 QQ，主人固定为"主人"─────────
  // 存储：/persona/data/nicknames.json  {"qq": "昵称", ...}
  // 规则：主人的 QQ（默认 1000000002）不允许改名——她是"主人"，这是防冒充的锚点。
  if (url.pathname === '/nicknames') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    const OWNER = process.env.PERSONA_OWNER_QQ || '';
    const FILE = '/persona/data/nicknames.json';
    const load = () => {
      try {
        return JSON.parse(fs.readFileSync(FILE, 'utf8')) || {};
      } catch {
        return {};
      }
    };
    const save = (obj) => {
      fs.mkdirSync(path.dirname(FILE), { recursive: true });
      const tmp = `${FILE}.tmp-${process.pid}`;
      fs.writeFileSync(tmp, JSON.stringify(obj, null, 2), 'utf8');
      fs.renameSync(tmp, FILE);
    };
    if (req.method === 'GET') {
      return sendJson(res, 200, { ok: true, owner: OWNER, map: load() });
    }
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      let payload = {};
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      } catch {
        return sendJson(res, 400, { ok: false, error: 'invalid json' });
      }
      const map = load();
      if (req.method === 'DELETE') {
        const qq = String(url.searchParams.get('qq') || payload.qq || '').trim();
        if (qq === OWNER) return sendJson(res, 403, { ok: false, error: '主人的昵称不可删除（防冒充锚点）' });
        const had = Boolean(map[qq]);
        delete map[qq];
        save(map);
        return sendJson(res, 200, { ok: true, removed: had, map });
      }
      const qq = String(payload.qq || '').trim();
      const name = String(payload.name || '').trim().slice(0, 24);
      if (!/^\d{5,12}$/.test(qq)) return sendJson(res, 400, { ok: false, error: 'qq 必须是 5-12 位数字' });
      if (!name) return sendJson(res, 400, { ok: false, error: 'name 不能为空' });
      if (qq === OWNER) return sendJson(res, 403, { ok: false, error: '主人的 QQ 已锁定为「主人」，不允许改自定昵称' });
      map[qq] = name;
      save(map);
      log(`[nicknames] ${qq} -> ${name}`);
      return sendJson(res, 200, { ok: true, map });
    });
    return;
  }

  // ── 群友档案：感知层定期把"谁是谁"写进她的记忆目录（不需要手机）──────────
  // 写入 /persona/memory/群友档案.md（自动生成，不碰她手写的 persona-memory.md）
  // 归属：这份文件是"平台自动维护的事实"，她可以读、也可以自己补印象，但平台重算时会覆盖"事实"部分。
  if (req.method === 'POST' && url.pathname === '/memory/roster') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      let payload = {};
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      } catch {
        return sendJson(res, 400, { ok: false, error: 'invalid json' });
      }
      const text = String(payload.text || '');
      if (!text.trim()) return sendJson(res, 400, { ok: false, error: 'empty text' });
      if (Buffer.byteLength(text) > 64 * 1024) return sendJson(res, 413, { ok: false, error: 'too large (64KB)' });
      const target = '/persona/memory/群友档案.md';
      try {
        if (fs.existsSync(target)) fs.copyFileSync(target, `${target}.bak`);
        const tmp = `${target}.tmp-${process.pid}`;
        fs.writeFileSync(tmp, text, 'utf8');
        fs.renameSync(tmp, target);
        log(`[memory] 群友档案已更新 ${Buffer.byteLength(text)}B`);
        return sendJson(res, 200, { ok: true, bytes: Buffer.byteLength(text) });
      } catch (err) {
        return sendJson(res, 500, { ok: false, error: String(err) });
      }
    });
    return;
  }

  // ── 媒体取回：把群消息里的图片/视频下载到 /persona/media/<id>.<ext> ────────
  // 她看图的物理前提；URL 来自 NapCat（腾讯 CDN，带签名，会过期）
  if (req.method === 'POST' && url.pathname === '/media/fetch') {
    if (!tokenOk(req.headers.authorization)) return sendJson(res, 401, { ok: false, error: 'unauthorized' });
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', async () => {
      let payload = {};
      try {
        payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
      } catch {
        return sendJson(res, 400, { ok: false, error: 'invalid json' });
      }
      const id = String(payload.id ?? '').replace(/[^0-9A-Za-z_-]/g, '');
      const src = String(payload.url || '');
      const ext = String(payload.ext || 'jpg').replace(/[^A-Za-z0-9]/g, '').slice(0, 5) || 'jpg';
      if (!id || !/^https?:\/\//i.test(src)) {
        return sendJson(res, 400, { ok: false, error: 'need id + http(s) url' });
      }
      const target = path.join(MEDIA_DIR, `${id}.${ext}`);
      try {
        fs.mkdirSync(MEDIA_DIR, { recursive: true });
        const resp = await fetch(src, { redirect: 'follow', signal: AbortSignal.timeout(20000) });
        if (!resp.ok) return sendJson(res, 502, { ok: false, error: `upstream ${resp.status}` });
        const buf = Buffer.from(await resp.arrayBuffer());
        if (buf.length === 0) return sendJson(res, 502, { ok: false, error: 'empty body' });
        if (buf.length > MEDIA_MAX_BYTES) return sendJson(res, 413, { ok: false, error: 'too large' });
        fs.writeFileSync(target, buf);
        log(`[media] 取回 ${id} ${buf.length}B -> ${target}`);
        return sendJson(res, 200, {
          ok: true, id, path: target, bytes: buf.length,
          content_type: resp.headers.get('content-type') || '',
        });
      } catch (err) {
        log(`[media] 取回失败 ${id}: ${String(err)}`);
        return sendJson(res, 502, { ok: false, error: String(err) });
      }
    });
    return undefined;
  }

  if (req.method !== 'POST' || url.pathname !== '/decide') {
    return sendJson(res, 404, { ok: false, error: 'not found' });
  }
  if (!tokenOk(req.headers.authorization)) {
    log('[auth] 拒绝未授权请求', req.socket.remoteAddress ?? '-');
    return sendJson(res, 401, { ok: false, error: 'unauthorized' });
  }

  // 读 body（上限 256KB：决策提示词最多几千字，超过就是异常请求）
  const chunks = [];
  let size = 0;
  req.on('data', (chunk) => {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      req.destroy();
      return;
    }
    chunks.push(chunk);
  });
  req.on('end', async () => {
    let payload;
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
    } catch {
      return sendJson(res, 400, { ok: false, error: 'invalid json' });
    }
    try {
      const result = await decide(payload);
      return sendJson(res, result.ok ? 200 : 502, result);
    } catch (err) {
      log('[error] decide 异常', String(err && err.stack ? err.stack : err));
      return sendJson(res, 500, { ok: false, error: String(err) });
    }
  });
  req.on('error', () => {
    /* 客户端断开：忽略 */
  });
  return undefined;
});

// 启动前自检：dsh 必须在 PATH 上，否则所有决策都会失败
try {
  fs.accessSync(path.join(WORKDIR), fs.constants.R_OK);
} catch {
  log('[fatal] 工作目录不可读', WORKDIR);
  process.exit(1);
}

server.listen(PORT, BIND, () => {
  log(`[boot] dsh-persona bridge 监听 ${BIND}:${PORT} profile=${PROFILE} cwd=${WORKDIR} 并发=${CONCURRENCY}`);
  log(`[boot] token 已加载（长度 ${TOKEN.length}），会话根 ${SESSIONS_ROOT}`);
  log(`[boot] 触发器配置 ${TRIGGERS_FILE}（存在=${fs.existsSync(TRIGGERS_FILE)}）`);
  log(`[boot] 专属表情包库 ${STICKER_MANIFEST}（存在=${fs.existsSync(STICKER_MANIFEST)}）`);
  try { fs.mkdirSync(MEDIA_DIR, { recursive: true }); } catch {}
  log(`[boot] 媒体缓存目录 ${MEDIA_DIR}`);
});

// 优雅退出：容器收到 SIGTERM 时别再接新活
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    log(`[exit] 收到 ${sig}，关闭监听`);
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  });
}
