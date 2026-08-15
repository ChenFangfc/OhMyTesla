"""语言与单位偏好。

设计要点：**语言和单位是两件事，必须解耦**。
加拿大用英文但用公里和摄氏度；英国用英里但用摄氏度。
所以语言只决定"默认"单位，用户可以逐项覆盖。

数据层永远是公制（TeslaMate 数据库里就是 km / °C / kW），
换算只发生在展示层——网页、推送文案、导出视频叠加层。
"""

import logging

from ..alerts.store import store

log = logging.getLogger(__name__)

LOCALES = ("zh", "en")

# 语言 → 默认单位。用户可逐项覆盖。
LOCALE_DEFAULTS = {
    "zh": {"length": "km", "temp": "C", "pressure": "bar", "currency": "CNY"},
    "en": {"length": "mi", "temp": "F", "pressure": "psi", "currency": "USD"},
}

# 市场 → 默认单位（比语言更准；加拿大是英文但用公制，这就是必须分开的原因）
MARKET_DEFAULTS = {
    "US": {"length": "mi", "temp": "F", "pressure": "psi", "currency": "USD"},
    "CA": {"length": "km", "temp": "C", "pressure": "psi", "currency": "CAD"},  # 英文 + 公制
    "GB": {"length": "mi", "temp": "C", "pressure": "psi", "currency": "GBP"},  # 英里 + 摄氏度
    "CN": {"length": "km", "temp": "C", "pressure": "bar", "currency": "CNY"},
    "EU": {"length": "km", "temp": "C", "pressure": "bar", "currency": "EUR"},
}

_DEFAULT = {
    "locale": "zh",
    "length": "km",
    "temp": "C",
    "pressure": "bar",
    "currency": "USD",   # 显示货币；电价方案里存的数值本身不带币种
    "auto": True,        # 单位跟随检测到的市场；手动改单位后置 False
    "market": "",        # 检测结果，只读展示用
    "locale_set": False, # 用户是否显式选过语言（防止每台设备各猜一次）
}


async def get() -> dict:
    saved = await store.get_setting("prefs", {}) or {}
    out = dict(_DEFAULT)
    out.update({k: v for k, v in saved.items() if k in _DEFAULT})
    return out


async def set_prefs(patch: dict) -> dict:
    cur = await get()
    # currency 以前不在这里：唯一能改它的途径是市场自动检测，用户手改不了。
    # 于是「电价方案里选了 EUR」和「fmt.money 用的显示币种」可以长期不一致，
    # 同一页上两个金额跟着两种符号。
    for k in ("locale", "length", "temp", "pressure", "currency"):
        if k in patch and patch[k]:
            cur[k] = str(patch[k])
    if "locale" in patch and patch["locale"]:
        cur["locale_set"] = True
    if "auto" in patch:
        cur["auto"] = bool(patch["auto"])
    if cur["locale"] not in LOCALES:
        cur["locale"] = "zh"
    if cur["length"] not in ("km", "mi"):
        cur["length"] = "km"
    if cur["temp"] not in ("C", "F"):
        cur["temp"] = "C"
    if cur["pressure"] not in ("bar", "psi", "kpa"):
        cur["pressure"] = "bar"
    # ISO 4217 是三位字母；别的一律回落，免得把乱码写进显示层
    code = str(cur.get("currency") or "").upper()
    cur["currency"] = code if (len(code) == 3 and code.isalpha()) else "USD"
    await store.set_setting("prefs", cur)
    return cur


async def apply_market(market: str) -> dict:
    """检测到市场后，在用户没手动改过的前提下套用该市场的默认**单位**。

    只改单位，**不改语言**——市场能可靠推断单位（美国用英里、中国用公里），
    但推断不了语言：在北美的中文车主要的是中文界面 + 英里。
    语言由用户自己选（顶栏一键切换），或首次访问时按浏览器语言。
    """
    cur = await get()
    cur["market"] = market
    if cur.get("auto") and market in MARKET_DEFAULTS:
        cur.update(MARKET_DEFAULTS[market])
    await store.set_setting("prefs", cur)
    return cur


async def locale() -> str:
    return (await get())["locale"]


# ---------------- 换算（数据层永远公制，这里只管显示）----------------

def km(value, unit: str, digits: int = 1):
    if value is None:
        return None
    v = float(value)
    return round(v * 0.621371 if unit == "mi" else v, digits)


def celsius(value, unit: str, digits: int = 1):
    if value is None:
        return None
    v = float(value)
    return round(v * 9 / 5 + 32 if unit == "F" else v, digits)


def bar(value, unit: str, digits: int = 1):
    if value is None:
        return None
    v = float(value)
    if unit == "psi":
        return round(v * 14.5038, digits)
    if unit == "kpa":
        return round(v * 100, 0)
    return round(v, 2)


def label(kind: str, prefs: dict) -> str:
    """单位后缀文本。"""
    return {
        "length": "mi" if prefs["length"] == "mi" else "km",
        "speed": "mph" if prefs["length"] == "mi" else "km/h",
        "temp": "°F" if prefs["temp"] == "F" else "°C",
        "pressure": {"psi": "psi", "kpa": "kPa"}.get(prefs["pressure"], "bar"),
        # 能耗：英里制用 Wh/mi，公制用 Wh/km
        "consumption": "Wh/mi" if prefs["length"] == "mi" else "Wh/km",
    }.get(kind, "")
