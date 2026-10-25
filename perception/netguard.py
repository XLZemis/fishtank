"""感知层 · 出站 URL / 目标地址策略（netguard.py）

为什么需要它（2026-10-25 安全复核）：
    · 通用网页抓取（browser.screenshot / browser.open_text）原来只校验 `http(s)` 前缀，
      任何群友都可以诱导她去打开 `http://172.18.0.x:port/...` 这类内网地址；
    · 发送文件（sender.send_file）会把任意 http(s) URL 交给 NapCat 代下载，
      同样能打内网；
    · 浏览器里还存着 QQ 空间 / B 站的登录态，任意网页共用同一个 context 会扩大风险。

本模块只做一件事：判断"这个 URL 能不能出去"，并给出可读的拒绝原因。原则：
    1. 只允许 http(s)，拒绝 file/ftp/gopher/chrome 等协议；
    2. 默认只允许公网地址：拒绝回环、私网、链路本地、组播、保留、元数据等；
    3. 允许显式白名单（登录态域、QQ CDN），白名单按"域及其子域"匹配；
    4. 解析 DNS 后再判定，避免 `http://127.0.0.1` 之外的花样域名；
    5. 解析结果做短缓存，避免一页几十个子资源都查一次 DNS。

设计约束：
    · 不 import NoneBot，便于在容器外的单元测试里直接加载；
    · 不抛异常：所有失败都通过 `Decision(ok=False, reason=...)` 返回；
    · 端口默认只放行 80/443；子资源抓取可传 `ports=None` 关闭端口限制（协议仍校验）。
"""
from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import urlsplit

# ── 常量 ──────────────────────────────────────────────────────────────────────
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = frozenset({80, 443})
MAX_URL_CHARS = 4096

# 明确拒绝的网段（除了 Python ipaddress 自带的 private/loopback/link_local 等判断，
# 这里再补上云厂商元数据与 CGNAT 等容易被忽视的段）。
_BLOCKED_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",          # 本网络
        "10.0.0.0/8",         # 私网
        "100.64.0.0/10",      # CGNAT（阿里云/腾讯云元数据也在类似段附近）
        "127.0.0.0/8",        # 回环
        "169.254.0.0/16",     # 链路本地 / 云元数据 169.254.169.254
        "172.16.0.0/12",      # 私网（docker 默认网段就在其中）
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",     # 私网
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",        # 组播
        "240.0.0.0/4",        # 保留
        "255.255.255.255/32",
        # IPv6
        "::/128",
        "::1/128",
        "64:ff9b::/96",       # NAT64
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",           # ULA
        "fe80::/10",          # 链路本地
        "ff00::/8",           # 组播
    )
)

# 主机名层面的硬拒绝（DNS 之前就能判定）
_BLOCKED_HOSTS = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.tencentyun.com",
    "instance-data",
})

# 解析缓存：host -> (过期时间, [ip...])
_RESOLVE_TTL_SECONDS = 600
_resolve_cache: dict[str, tuple[float, list[str]]] = {}


@dataclass(frozen=True)
class Decision:
    """策略结论。ok=False 时 reason 是给日志/审计看的中文原因。"""

    ok: bool
    reason: str = ""
    host: str = ""
    ip: str = ""


def _host_matches(host: str, pattern: str) -> bool:
    """白名单匹配：pattern 本身或其子域。例：bilibili.com 匹配 www.bilibili.com。"""
    host = host.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")
    return host == pattern or host.endswith("." + pattern)


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def blocked_ip(value: str) -> bool:
    """True = 这个 IP 不允许出站。"""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return True  # 解析不出来的一律当危险处理（fail-closed）
    if any(ip in net for net in _BLOCKED_NETS):
        return True
    # ipaddress 自带的判断做兜底（不同 Python 版本覆盖面略有差异）
    return bool(
        getattr(ip, "is_private", False)
        or getattr(ip, "is_loopback", False)
        or getattr(ip, "is_link_local", False)
        or getattr(ip, "is_multicast", False)
        or getattr(ip, "is_reserved", False)
        or getattr(ip, "is_unspecified", False)
    )


def resolve_host(host: str, *, resolver=None, use_cache: bool = True) -> list[str]:
    """解析主机名为 IP 字符串列表；失败返回空列表（不抛异常）。

    resolver 参数用于单测注入（签名与 socket.getaddrinfo 一致）。
    """
    key = host.lower().rstrip(".")
    now = time.monotonic()
    if use_cache:
        hit = _resolve_cache.get(key)
        if hit and hit[0] > now:
            return list(hit[1])
    fn = resolver or socket.getaddrinfo
    try:
        infos = fn(key, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except Exception:  # noqa: BLE001 - 解析失败就是失败
        return []
    ips: list[str] = []
    for info in infos or []:
        try:
            addr = info[4][0]
        except (IndexError, TypeError):
            continue
        if addr and addr not in ips:
            ips.append(str(addr))
    if use_cache and ips:
        _resolve_cache[key] = (now + _RESOLVE_TTL_SECONDS, ips)
    return ips


def clear_cache() -> None:
    """测试/配置变更后清缓存。"""
    _resolve_cache.clear()


def check_url(
    url: str,
    *,
    allow_private: bool = False,
    allow_hosts: Iterable[str] = (),
    ports: Sequence[int] | None = DEFAULT_PORTS,
    resolver=None,
) -> Decision:
    """判断 URL 是否允许出站。

    参数：
        url            目标地址
        allow_private  显式允许私网（仅给受信任的内部调用用，默认 False）
        allow_hosts    域名白名单（含子域）；命中后跳过私网/端口限制
        ports          允许的端口；None = 不限制端口（协议仍校验）
        resolver       DNS 解析函数（测试注入）
    """
    raw = str(url or "").strip()
    if not raw:
        return Decision(False, "url 为空")
    if len(raw) > MAX_URL_CHARS:
        return Decision(False, "url 过长")
    try:
        parts = urlsplit(raw)
    except ValueError:
        return Decision(False, "url 解析失败")
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        return Decision(False, f"协议不允许：{scheme or '(空)'}")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return Decision(False, "缺少主机名")
    if parts.username or parts.password:
        return Decision(False, "url 不允许携带用户信息")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return Decision(False, f"主机名被拒绝：{host}", host=host)

    if any(_host_matches(host, p) for p in allow_hosts):
        return Decision(True, "白名单主机", host=host)

    # 端口：端口没写时用协议默认端口
    try:
        port = parts.port
    except ValueError:
        return Decision(False, "端口非法", host=host)
    if port is None:
        port = 443 if scheme == "https" else 80
    if ports is not None and port not in set(ports):
        return Decision(False, f"端口不允许：{port}", host=host)

    if allow_private:
        return Decision(True, "显式允许私网", host=host)

    # IP 字面量直接判定；域名解析后再判定（防 DNS 指向内网）
    if _is_ip_literal(host):
        return (
            Decision(False, f"地址不在公网范围：{host}", host=host, ip=host)
            if blocked_ip(host)
            else Decision(True, "公网 IP", host=host, ip=host)
        )
    ips = resolve_host(host, resolver=resolver)
    if not ips:
        return Decision(False, f"域名解析失败：{host}", host=host)
    for ip in ips:
        if blocked_ip(ip):
            return Decision(False, f"域名解析到受限地址：{host} -> {ip}", host=host, ip=ip)
    return Decision(True, "公网域名", host=host, ip=ips[0])
