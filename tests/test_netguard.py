import unittest

from helpers import load_perception

netguard = load_perception("netguard")


def _resolver(mapping):
    """构造一个 getaddrinfo 形状的假解析器（测试用，不走真实 DNS）。"""

    def resolver(host, *_args, **_kwargs):
        ips = mapping.get(host)
        if not ips:
            raise OSError("no such host")
        return [(None, None, None, "", (ip, 0)) for ip in ips]

    return resolver


class TestCheckUrl(unittest.TestCase):
    def setUp(self):
        netguard.clear_cache()

    def test_private_and_loopback_are_rejected(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.1.2.3:80/",
            "http://172.18.0.7:6099/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/",
            "http://[::1]/",
            "http://[fe80::1]/",
        ):
            decision = netguard.check_url(url)
            self.assertFalse(decision.ok, url)

    def test_public_ip_allowed(self):
        decision = netguard.check_url("http://1.1.1.1/")
        self.assertTrue(decision.ok, decision.reason)

    def test_scheme_is_limited(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://1.1.1.1/"):
            self.assertFalse(netguard.check_url(url).ok, url)
        self.assertTrue(netguard.check_url("https://1.1.1.1/").ok)

    def test_localhost_and_blocked_hostnames(self):
        for url in ("http://localhost/x", "http://metadata.google.internal/", "http://app.localhost/"):
            self.assertFalse(netguard.check_url(url).ok, url)

    def test_ports_restricted_by_default(self):
        self.assertFalse(netguard.check_url("http://1.1.1.1:8080/").ok)
        self.assertTrue(netguard.check_url("http://1.1.1.1:8080/", ports=None).ok)
        self.assertTrue(netguard.check_url("https://1.1.1.1/").ok)

    def test_userinfo_rejected(self):
        self.assertFalse(netguard.check_url("http://user:pass@1.1.1.1/").ok)

    def test_domain_resolving_to_private_is_rejected(self):
        decision = netguard.check_url(
            "http://evil.example/",
            resolver=_resolver({"evil.example": ["10.0.0.5"]}),
        )
        self.assertFalse(decision.ok)
        self.assertIn("受限地址", decision.reason)

    def test_public_domain_allowed(self):
        decision = netguard.check_url(
            "https://example.com/",
            resolver=_resolver({"example.com": ["93.184.216.34"]}),
        )
        self.assertTrue(decision.ok, decision.reason)

    def test_allow_hosts_skips_ip_and_port_checks(self):
        for url in ("https://user.qzone.qq.com/1", "https://www.bilibili.com/video/BV1xx"):
            decision = netguard.check_url(url, allow_hosts=("qzone.qq.com", "bilibili.com"))
            self.assertTrue(decision.ok, url)

    def test_unresolvable_domain_is_rejected(self):
        self.assertFalse(netguard.check_url("http://nx.example/", resolver=_resolver({})).ok)

    def test_allow_private_is_explicit(self):
        self.assertTrue(netguard.check_url("http://10.0.0.5/", allow_private=True).ok)

    def test_empty_and_oversized_urls(self):
        self.assertFalse(netguard.check_url("").ok)
        self.assertFalse(netguard.check_url("http://1.1.1.1/" + "a" * 5000).ok)


if __name__ == "__main__":
    unittest.main()
