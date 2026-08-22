"""多渠道通知发送。

内置四条快捷通道（SMTP 邮件 / ntfy / Telegram / 通用 Webhook），
外加 APPRISE_URLS 直通 Apprise 的 100+ 服务。
Apprise 是同步库，统一丢进线程池执行。
"""

import asyncio
import logging
from urllib.parse import quote, urlparse

import apprise
import httpx

from ..config import settings

log = logging.getLogger(__name__)


def _email_ready() -> bool:
    """SMTP 是不是真的能用。

    只看 host 非空会把「填了服务器但还没填密码」当成已配置 —— 界面显示
    "Delivering via Email"，实际一封也发不出去。安防产品最怕的就是这种
    看起来在工作的失效。
    """
    if not (settings.smtp_host and settings.smtp_to):
        return False
    # 公共邮件服务一律要认证；自建中继可能允许匿名，那种情况 user 也会留空
    needs_auth = any(
        settings.smtp_host.endswith(d)
        for d in ("gmail.com", "qq.com", "163.com", "outlook.com", "office365.com",
                  "yahoo.com", "126.com", "icloud.com", "zoho.com", "sendgrid.net")
    )
    if needs_auth and not (settings.smtp_user and settings.smtp_pass):
        return False
    return True


def _build_apprise_urls() -> list[str]:
    urls: list[str] = []

    # 必须和 configured_channels() 用同一个判断。之前这里只看 host+to，
    # 于是密码为空的邮件 URL 照样被拼进去，Apprise 认证失败会把**整次发送**
    # 判为失败 —— 连本来好好的 ntfy 也跟着不发了。
    if _email_ready():
        user = quote(settings.smtp_user, safe="")
        pw = quote(settings.smtp_pass, safe="")
        scheme = "mailtos" if settings.smtp_port in (465, 587) else "mailto"
        url = (
            f"{scheme}://{user}:{pw}@{settings.smtp_host}:{settings.smtp_port}"
            f"/?to={quote(settings.smtp_to, safe='')}"
        )
        if settings.smtp_from:
            url += f"&from={quote(settings.smtp_from, safe='')}"
        if settings.smtp_port == 465:
            url += "&mode=ssl"  # 465 是隐式 TLS（QQ/163 默认端口）
        elif settings.smtp_port == 587:
            url += "&mode=starttls"
        urls.append(url)

    if settings.ntfy_topic:
        base = settings.ntfy_url or "https://ntfy.sh"
        parsed = urlparse(base if "://" in base else f"https://{base}")
        scheme = "ntfys" if parsed.scheme == "https" else "ntfy"
        host = parsed.netloc or parsed.path
        urls.append(f"{scheme}://{host}/{settings.ntfy_topic}")

    if settings.telegram_bot_token and settings.telegram_chat_id:
        urls.append(f"tgram://{settings.telegram_bot_token}/{settings.telegram_chat_id}")

    if settings.apprise_urls:
        urls.extend(u.strip() for u in settings.apprise_urls.split(",") if u.strip())

    return urls




def email_config_problem() -> str:
    """邮件没配全时说明缺哪一项，供设置页显示。"""
    if not settings.smtp_host:
        return ""
    if not settings.smtp_to:
        return "missing_to"
    if not _email_ready():
        return "missing_password" if settings.smtp_user else "missing_user"
    return ""


async def configured_channels() -> list[str]:
    channels = []
    if _email_ready():
        channels.append("email")
    if settings.ntfy_topic:
        channels.append("ntfy")
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append("telegram")
    if settings.alert_webhook_url:
        channels.append("webhook")
    if settings.apprise_urls:
        channels.append("apprise")
    try:
        from . import webpush

        if await webpush.count():
            channels.append("webpush")
    except Exception:  # noqa: BLE001
        pass
    return channels


def _send_apprise_sync(title: str, body: str, attach: str | None) -> bool:
    urls = _build_apprise_urls()
    if not urls:
        return False
    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    kwargs = {}
    if attach:
        kwargs["attach"] = attach
    return bool(ap.notify(title=title, body=body, **kwargs))


async def send(title: str, body: str, payload: dict | None = None, attach: str | None = None) -> tuple[bool, list[str]]:
    """向所有已配置渠道发送；返回 (至少一个成功, 渠道列表)。"""
    channels = await configured_channels()
    if not channels:
        log.warning("no notification channel configured; alert dropped: %s", title)
        return False, []

    ok = False

    if "webpush" in channels:
        try:
            from . import webpush

            # /alerts 是旧路径，会 301 到 /security。推送里给的链接直接给终点，
            # 别让手机上点开先吃一次跳转。
            url = "/security"
            if payload and payload.get("rule", "").startswith(("sentry_", "new_sentry")):
                url = "/dashcam"
            ok = await webpush.send(
                title, body, url=url, level=(payload or {}).get("level", "info")
            ) or ok
        except Exception:
            log.exception("web push send failed")

    if any(c in channels for c in ("email", "ntfy", "telegram", "apprise")):
        try:
            ok = await asyncio.to_thread(_send_apprise_sync, title, body, attach) or ok
        except Exception:
            log.exception("apprise send failed")

    if settings.alert_webhook_url:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    settings.alert_webhook_url,
                    json={"title": title, "body": body, **(payload or {})},
                )
                ok = ok or resp.is_success
        except Exception:
            log.exception("webhook send failed")

    return ok, channels
