import assert from 'node:assert/strict';
import http from 'node:http';
import test from 'node:test';

import { clearCache, fetchGuarded, hostAllowed, isBlockedIp, looksLikeMedia } from './netguard.mjs';

const JPEG = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(32)]);
const PNG = Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), Buffer.alloc(32)]);

test('isBlockedIp 拒绝内网/回环/链路本地/元数据地址', () => {
  for (const ip of [
    '127.0.0.1', '10.1.2.3', '172.18.0.3', '192.168.1.1', '169.254.169.254',
    '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', '::', 'fc00::1', 'fe80::1', 'ff02::1',
    '::ffff:127.0.0.1',
  ]) {
    assert.equal(isBlockedIp(ip), true, `${ip} 应被拒绝`);
  }
});

test('isBlockedIp 放行公网地址', () => {
  for (const ip of ['1.1.1.1', '8.8.8.8', '8.8.4.4', '2606:4700:4700::1111']) {
    assert.equal(isBlockedIp(ip), false, `${ip} 应放行`);
  }
});

test('hostAllowed 拒绝 localhost 与私网字面量', async () => {
  assert.equal((await hostAllowed('localhost')).allowed, false);
  assert.equal((await hostAllowed('127.0.0.1')).allowed, false);
  assert.equal((await hostAllowed('172.18.0.7')).allowed, false);
  assert.equal((await hostAllowed('')).allowed, false);
});

test('hostAllowed 白名单命中（含子域）', async () => {
  const gate = await hostAllowed('user.qzone.qq.com', { allowHosts: ['qzone.qq.com'] });
  assert.equal(gate.allowed, true);
  const sub = await hostAllowed('www.bilibili.com', { allowHosts: ['bilibili.com'] });
  assert.equal(sub.allowed, true);
});

test('hostAllowed 公网 IP 放行', async () => {
  const gate = await hostAllowed('1.1.1.1');
  assert.equal(gate.allowed, true);
});

test('fetchGuarded 直接拒绝内网 URL（不发请求）', async () => {
  await assert.rejects(
    () => fetchGuarded('http://172.18.0.7:6099/'),
    (err) => err.code === 'blocked',
  );
  await assert.rejects(
    () => fetchGuarded('file:///etc/passwd'),
    (err) => err.code === 'blocked',
  );
  await assert.rejects(
    () => fetchGuarded('http://user:pass@example.com/'),
    (err) => err.code === 'blocked',
  );
});

test('looksLikeMedia 魔数识别', () => {
  assert.equal(looksLikeMedia(JPEG), true);
  assert.equal(looksLikeMedia(PNG), true);
  assert.equal(looksLikeMedia(Buffer.from('<html><body>no</body></html>')), false);
  assert.equal(looksLikeMedia(Buffer.alloc(2)), false);
});

test('缓存可清空', () => {
  clearCache();
  assert.ok(true);
});


test('fetchGuarded 拒绝重定向跳到内网（逐跳校验）', async () => {
  const srv = http.createServer((_req, res) => {
    res.writeHead(302, { location: 'http://10.0.0.9/secret' });
    res.end();
  });
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const port = srv.address().port;
  try {
    await assert.rejects(
      () => fetchGuarded(`http://127.0.0.1:${port}/`, { allowHosts: ['127.0.0.1'] }),
      (err) => err.code === 'blocked' && /(受限地址|不在公网范围)/.test(err.message),
    );
  } finally {
    await new Promise((resolve) => srv.close(resolve));
  }
});

test('fetchGuarded 流式限量（超限即 abort）', async () => {
  const srv = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'image/png' });
    // 持续写 1KB 块，直到被取消
    const chunk = Buffer.alloc(1024, 1);
    const timer = setInterval(() => res.write(chunk), 2);
    res.on('close', () => clearInterval(timer));
  });
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const port = srv.address().port;
  try {
    await assert.rejects(
      () => fetchGuarded(`http://127.0.0.1:${port}/`, { allowHosts: ['127.0.0.1'], maxBytes: 4096 }),
      (err) => err.code === 'limit',
    );
  } finally {
    await new Promise((resolve) => srv.close(resolve));
  }
});

test('fetchGuarded content-type 白名单拒绝 HTML', async () => {
  const srv = http.createServer((_req, res) => {
    res.writeHead(200, { 'content-type': 'text/html' });
    res.end('<html><body>hi</body></html>');
  });
  await new Promise((resolve) => srv.listen(0, '127.0.0.1', resolve));
  const port = srv.address().port;
  try {
    await assert.rejects(
      () => fetchGuarded(`http://127.0.0.1:${port}/`, {
        allowHosts: ['127.0.0.1'],
        allowContentTypes: ['image/', 'video/'],
      }),
      (err) => err.code === 'blocked',
    );
  } finally {
    await new Promise((resolve) => srv.close(resolve));
  }
});
