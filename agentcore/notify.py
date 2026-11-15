"""通知渠道:Server酱(微信)、SMTP 邮件、Windows 桌面通知(尽力而为)、日志兜底。
任务失败/日报经此发出。纯标准库。"""
import json
import logging
import smtplib
import subprocess
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

log = logging.getLogger("notify")


def send(channels_cfg, title, body):
    """按配置向所有启用的渠道发送通知,返回 {渠道: bool}。"""
    results = {}
    ch = (channels_cfg or {}).get("channels", {}) or {}
    if ch.get("serverchan", {}).get("enabled") and ch["serverchan"].get("sendkey"):
        results["serverchan"] = _serverchan(ch["serverchan"]["sendkey"], title, body)
    if ch.get("wecom", {}).get("enabled") and ch["wecom"].get("webhook"):
        results["wecom"] = _wecom_bot(ch["wecom"]["webhook"], title, body)
    if ch.get("dingtalk", {}).get("enabled") and ch["dingtalk"].get("webhook"):
        results["dingtalk"] = _dingtalk_bot(ch["dingtalk"]["webhook"], title, body)
    if ch.get("feishu", {}).get("enabled") and ch["feishu"].get("webhook"):
        results["feishu"] = _feishu_bot(ch["feishu"]["webhook"], title, body)
    if ch.get("email", {}).get("enabled"):
        results["email"] = email_send(ch["email"], ch["email"].get("to", ""), title, body)
    if ch.get("desktop", {}).get("enabled"):
        results["desktop"] = _desktop(title, body)
    if results:
        log.info("通知已发送 %s: %s", title, results)
    return results


def _guarded_urlopen(url, allowed_hosts, data=None, timeout=15):
    """SSRF 防护的受限请求:仅 https + 域名白名单 + 解析 IP 拒绝私网/环回 + 禁重定向。
    返回响应对象;URL 不合法或解析到受限地址时返回 None。"""
    import ipaddress
    import socket
    import urllib.parse
    u = urllib.parse.urlparse(url or "")
    if u.scheme != "https" or u.hostname not in allowed_hosts:
        log.warning("通知 URL 被拒(仅允许 https + %s): %s", allowed_hosts, u.hostname)
        return None
    try:
        for info in socket.getaddrinfo(u.hostname, 443, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                log.warning("域名解析到受限地址,已阻断: %s -> %s", u.hostname, ip)
                return None
    except Exception as e:
        log.warning("域名解析失败: %s", e)
        return None

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


# 公开别名:其他模块(inbox 等)访问官方 API 时复用同一套 SSRF 防护
guarded_urlopen = _guarded_urlopen


def email_send(cfg, to, title, body):
    """发一封邮件(to 可指定收件人;cfg 为 email 渠道配置)。"""
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = cfg.get("user", "")
        msg["To"] = to
        port = int(cfg.get("port", 465))
        if port == 465:
            server = smtplib.SMTP_SSL(cfg.get("smtp_host", "") or cfg.get("host", ""),
                                      port, timeout=15)
        else:
            server = smtplib.SMTP(cfg.get("smtp_host", "") or cfg.get("host", ""),
                                  port, timeout=15)
            if cfg.get("starttls", True):
                server.starttls()
        try:
            server.login(cfg.get("user", ""), cfg.get("password", ""))
            server.sendmail(cfg.get("user", ""), [to], msg.as_string())
        finally:
            server.close()
        return True
    except Exception as e:
        log.warning("邮件发送失败: %s", e)
        return False


def _serverchan(sendkey, title, body):
    try:
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9]+", sendkey or ""):
            log.warning("Server酱 sendkey 格式非法")
            return False
        url = "https://sctapi.ftqq.com/%s.send" % sendkey
        data = urllib.parse.urlencode({"title": title[:32], "desp": body[:3000]}).encode()
        resp = _guarded_urlopen(url, {"sctapi.ftqq.com"}, data=data)
        if resp is None:
            return False
        with resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("code") == 0
    except Exception as e:
        log.warning("Server酱发送失败: %s", e)
        return False


def _wecom_bot(webhook, title, body):
    """企业微信群机器人 webhook:一条 markdown 消息(域名白名单见 _guarded_urlopen)。"""
    try:
        data = json.dumps({"msgtype": "markdown",
                           "markdown": {"content": "**%s**\n%s" % (title[:64], body[:1800])}}).encode("utf-8")
        resp = _guarded_urlopen(webhook, {"qyapi.weixin.qq.com"}, data=data)
        if resp is None:
            return False
        with resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("errcode") == 0
    except Exception as e:
        log.warning("企业微信发送失败: %s", e)
        return False


def _dingtalk_bot(webhook, title, body):
    """钉钉群机器人 webhook:一条 markdown 消息(域名白名单见 _guarded_urlopen)。"""
    try:
        data = json.dumps({"msgtype": "markdown",
                           "markdown": {"title": title[:32],
                                        "text": "**%s**\n%s" % (title[:64], body[:1800])}}).encode("utf-8")
        resp = _guarded_urlopen(webhook, {"oapi.dingtalk.com"}, data=data)
        if resp is None:
            return False
        with resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("errcode") == 0
    except Exception as e:
        log.warning("钉钉发送失败: %s", e)
        return False


def _feishu_bot(webhook, title, body):
    """飞书群机器人 webhook:一条文本消息(飞书/国际版 lark 域名白名单)。"""
    try:
        data = json.dumps({"msg_type": "text",
                           "content": {"text": "**%s**\n%s" % (title[:64], body[:1800])}}).encode("utf-8")
        resp = _guarded_urlopen(webhook, {"open.feishu.cn", "open.larksuite.com"}, data=data)
        if resp is None:
            return False
        with resp:
            return json.loads(resp.read().decode("utf-8", "replace")).get("code") == 0
    except Exception as e:
        log.warning("飞书发送失败: %s", e)
        return False


def _desktop(title, body):
    """Windows 气泡通知:调 PowerShell(无第三方依赖),失败静默。"""
    if subprocess.run(["where", "powershell"], capture_output=True).returncode != 0:
        return False
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(8000,'%s','%s',[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Seconds 9;$n.Dispose()" % (title[:60].replace("'", " "), body[:200].replace("'", " ")))
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                           capture_output=True, timeout=20)
        return p.returncode == 0
    except Exception as e:
        log.warning("桌面通知失败: %s", e)
        return False
