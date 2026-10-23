#!/usr/bin/env python3
"""mail_send.py —— 给 mail_send 工具用的 SMTP 发信器。

用法：python3 mail_send.py '<json>'
JSON: {"to":"a@b.com,c@d.com","subject":"...","body":"...","attachments":["/persona/out/x.png"]}

配置只读环境变量（不写死、不落仓库）：
  FISHTANK_SMTP_HOST / PORT / USER / PASSWORD（或 PASSWORD_FILE）
  FISHTANK_SMTP_FROM / SECURITY(ssl|starttls|plain) / TIMEOUT
"""
from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

MAX_ATTACH_BYTES = 25 * 1024 * 1024
MAX_ONE_ATTACH = 20 * 1024 * 1024


def fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    return 1


def env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def password() -> str:
    value = env("FISHTANK_SMTP_PASSWORD")
    if value:
        return value
    path = env("FISHTANK_SMTP_PASSWORD_FILE")
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        return fail("缺少 JSON 参数")
    try:
        data = json.loads(sys.argv[1])
    except Exception:  # noqa: BLE001
        return fail("参数不是合法 JSON")

    to_raw = data.get("to") or ""
    if isinstance(to_raw, (list, tuple)):
        recipients = [str(x).strip() for x in to_raw if str(x).strip()]
    else:
        recipients = [x.strip() for x in str(to_raw).replace(";", ",").split(",") if x.strip()]
    if not recipients:
        return fail("缺少收件人 to")
    if any("@" not in x for x in recipients):
        return fail("收件人格式不正确")

    subject = str(data.get("subject") or "").strip() or "(无主题)"
    body = str(data.get("body") or "")
    attachments = data.get("attachments") or []
    if not isinstance(attachments, list):
        return fail("attachments 必须是数组")

    host = env("FISHTANK_SMTP_HOST")
    port_s = env("FISHTANK_SMTP_PORT", "465")
    user = env("FISHTANK_SMTP_USER")
    pwd = password()
    sender = env("FISHTANK_SMTP_FROM", user)
    if not host:
        return fail("SMTP 未配置：缺少 FISHTANK_SMTP_HOST")
    if not sender:
        return fail("SMTP 未配置：缺少 FISHTANK_SMTP_FROM/USER")
    try:
        port = int(port_s)
    except ValueError:
        return fail("FISHTANK_SMTP_PORT 不是整数")
    security = env("FISHTANK_SMTP_SECURITY", "ssl" if port == 465 else "starttls").lower()
    if security not in {"ssl", "starttls", "plain"}:
        return fail("FISHTANK_SMTP_SECURITY 只能是 ssl/starttls/plain")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    total = 0
    attached: list[str] = []
    for raw in attachments:
        p = Path(str(raw)).expanduser()
        try:
            p = p.resolve()
        except OSError:
            return fail(f"附件路径无效：{raw}")
        if not str(p).startswith("/persona/"):
            return fail(f"附件只允许 /persona 下的文件：{raw}")
        if not p.is_file():
            return fail(f"附件不存在：{raw}")
        size = p.stat().st_size
        if size > MAX_ONE_ATTACH:
            return fail(f"附件过大（>20MB）：{raw}")
        total += size
        if total > MAX_ATTACH_BYTES:
            return fail("附件总大小超过 25MB")
        ctype, _ = mimetypes.guess_type(str(p))
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype or "octet-stream", filename=p.name)
        attached.append(str(p))

    timeout = float(env("FISHTANK_SMTP_TIMEOUT", "45") or 45)
    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
        try:
            if user and pwd:
                server.login(user, pwd)
            elif user or pwd:
                return fail("SMTP USER/PASSWORD 必须同时配置")
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        return fail(f"SMTP 发送失败：{type(exc).__name__}: {exc}")

    print(json.dumps({"ok": True, "to": ", ".join(recipients), "attachments": attached}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
