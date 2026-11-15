"""接入渠道:让 HeartAgent 离开桌面也能收发消息 —— 邮件(IMAP 收 / SMTP 回)
与 Telegram Bot(long-poll 收 / API 回)。只响应白名单内的发件人,安全默认关闭。
入站消息交给主控AI无人值守处理,最终回答经原渠道回复。"""
import email
import imaplib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from email.header import decode_header, make_header

from . import notify


def _api_open(url, data=None):
    """访问平台官方 API 的唯一通道:https + 域名白名单 + IP 边界 + 禁重定向。"""
    host = urllib.parse.urlparse(url or "").hostname
    if host not in ("api.telegram.org", "api.weixin.qq.com"):
        raise RuntimeError("不允许的平台域名: %s" % host)
    resp = notify.guarded_urlopen(url, {host}, data=data)
    if resp is None:
        raise RuntimeError("平台 API 请求被安全守卫拒绝")
    return resp


def _decode_mime(s):
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _email_body(msg):
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get_filename():
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
            return ""
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


class InboxManager:
    """轮询式收件箱;cfg 每轮从 get_cfg() 读取,热重载即时生效。"""

    def __init__(self, brain, events, get_cfg):
        self.brain = brain
        self.events = events
        self.get_cfg = get_cfg  # lambda: app["cfg"].get("inbox", {})
        self._stop = threading.Event()
        self._tg_offset = 0
        self._thread = None
        self.stats = {"email_in": 0, "telegram_in": 0, "replied": 0, "ignored": 0}

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="inbox", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def status(self):
        cfg = self.get_cfg() or {}
        return {"email": bool(cfg.get("email", {}).get("enabled")),
                "telegram": bool(cfg.get("telegram", {}).get("enabled")),
                "webhook": bool(cfg.get("webhook", {}).get("enabled")),
                **self.stats}

    # ---------- 主循环 ----------
    def _loop(self):
        while not self._stop.wait(5):
            cfg = self.get_cfg() or {}
            poll_sec = max(20, int(cfg.get("poll_sec", 60)))
            try:
                if cfg.get("email", {}).get("enabled"):
                    self._poll_email(cfg["email"])
            except Exception as e:
                self.events.publish("error", text="邮件收件箱异常: %s" % e)
            try:
                if cfg.get("telegram", {}).get("enabled"):
                    self._poll_telegram(cfg["telegram"])
            except Exception as e:
                self.events.publish("error", text="Telegram 收件箱异常: %s" % e)
            self._stop.wait(poll_sec)

    # ---------- 邮件 ----------
    def _poll_email(self, cfg):
        host = (cfg.get("host") or "").strip()
        if not host:
            return
        allow = [a.strip().lower() for a in cfg.get("allow_from", []) if a.strip()]
        imap = imaplib.IMAP4_SSL(host, int(cfg.get("port", 993)))
        try:
            imap.login(cfg.get("user", ""), cfg.get("password", ""))
            imap.select("INBOX")
            typ, data = imap.uid("search", None, "UNSEEN")
            if typ != "OK":
                return
            uids = (data[0] or b"").split()[-5:]  # 每轮最多处理 5 封
            for uid in reversed(uids):
                typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                imap.uid("store", uid, "+FLAGS", "(\\Seen)")  # 处理过就标记,防重复
                msg = email.message_from_bytes(raw)
                sender = _decode_mime(msg.get("From", ""))
                addr = (re.search(r"<([^>]+)>", sender).group(1)
                        if re.search(r"<([^>]+)>", sender) else sender).strip().lower()
                subject = _decode_mime(msg.get("Subject", ""))
                if allow and not any(a in addr for a in allow):
                    self.stats["ignored"] += 1
                    continue
                body = _email_body(msg).strip()[:4000]
                self.stats["email_in"] += 1
                self.events.publish("inbound", source="邮件",
                                    text="来自 %s | %s\n%s" % (addr, subject, body[:300]))
                reply_to = addr
                out = self.brain.autonomous(
                    "收到一封邮件,请处理。发件人: %s\n主题: %s\n正文:\n%s\n\n"
                    "需要回复时,把完整的回复正文放在最终回答里(将原样邮件回复给发件人)。"
                    % (addr, subject, body),
                    source="邮件: %s" % addr)
                if out:
                    notify.email_send(cfg, reply_to,
                                      "Re: %s" % (subject or "HeartAgent 回复"), out)
                    self.stats["replied"] += 1
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    # ---------- Telegram ----------
    def _poll_telegram(self, cfg):
        token = (cfg.get("token") or "").strip()
        if not token:
            return
        allow = {str(c).strip() for c in cfg.get("allow_chat_ids", []) if str(c).strip()}
        url = ("https://api.telegram.org/bot%s/getUpdates?timeout=25&offset=%d"
               % (token, self._tg_offset))
        with _api_open(url) as r:
            data = json.loads(r.read().decode("utf-8"))
        for upd in (data.get("result") or [])[-10:]:
            self._tg_offset = max(self._tg_offset, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            text = str(msg.get("text", "")).strip()
            sender = str((msg.get("from") or {}).get("username")
                         or (msg.get("from") or {}).get("first_name", ""))
            if not text or not chat_id:
                continue
            if allow and chat_id not in allow:
                self.stats["ignored"] += 1
                continue
            self.stats["telegram_in"] += 1
            self.events.publish("inbound", source="Telegram",
                                text="来自 %s(%s)\n%s" % (sender or "未知", chat_id, text[:300]))
            out = self.brain.autonomous(
                "收到一条 Telegram 消息,请处理并直接给出要回复的内容。\n发送者: %s\n消息:\n%s"
                % (sender or chat_id, text[:3000]),
                source="TG: %s" % (sender or chat_id))
            if out:
                self._tg_send(token, chat_id, out[:3500])
                self.stats["replied"] += 1

    @staticmethod
    def _tg_send(token, chat_id, text):
        body = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token, data=body)
        with _api_open(req.full_url, data=body) as r:
            r.read()
