"""市场（区域）自动识别。

按可信度从高到低：

1. **Token 里 JWT 的签发方域名**——最权威。特斯拉账号注册在哪个区，
   token 的 `iss` 就是那个区的认证服务器（auth.tesla.cn vs auth.tesla.com）。
   TeslaMate 判断中国区用的就是这个（lib/tesla_api/auth.ex 的 region/1）。
2. **车机自己的单位设置**（vehicle_config / gui_settings 里的 gui_distance_units）——
   直接告诉你车主看的是英里还是公里，但 TeslaMate 不落库，只能在有 token 时现查。
3. **VIN 的前三位**（WMI）——只说明"在哪造的"，不等于"卖到哪"：
   上海产的车大量出口欧洲，所以它只能当兜底。
4. **浏览器语言**——只影响界面默认语言，不影响单位。

识别结果只用来决定**默认值**；用户手动改过之后就不再自动覆盖。
"""

import base64
import json
import logging

log = logging.getLogger(__name__)

# 地址里的国家名 → 市场码。TeslaMate 的 addresses.country 是
# Nominatim 返回的本地化国家名，所以中英两种写法都要认。
_COUNTRY_CODE = {
    "united states": "US", "usa": "US", "美国": "US",
    "canada": "CA", "加拿大": "CA",
    "united kingdom": "GB", "英国": "GB",
    "china": "CN", "中国": "CN",
}

# VIN 前三位 → 生产地（注意：产地 ≠ 销售市场）
# 只用于展示"这台车在哪造的"，不参与市场判定。
# 上海产的车大量出口欧洲/亚太，弗里蒙特产的也进口到中国，用它判市场两个方向都会错。
WMI_PLANT = {
    "5YJ": "Fremont, California",
    "7SA": "Austin, Texas",
    "7G2": "Austin, Texas",       # 已在实机 VIN 上确认
    "LRW": "Shanghai, China",
    "XP7": "Berlin, Germany",
}


def _jwt_payload(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        raw = parts[1]
        raw += "=" * (-len(raw) % 4)  # base64url 补齐
        return json.loads(base64.urlsafe_b64decode(raw))
    except Exception:  # noqa: BLE001
        return None


# JWT 的 ou_code 声明 → 大区
_OU_MARKET = {"CN": "CN", "NA": "NA", "EU": "EU"}


def from_token(token: str | None) -> tuple[str | None, str | None]:
    """解析 access token 的 JWT。返回 (大区, 账号语言)。

    优先看 `ou_code` 声明（NA / EU / CN），比签发方域名更直接；
    再退回签发方域名后缀（TeslaMate 用的就是这个）。
    `locale` 声明（如 zh-CN）只作为**语言**建议，不用来定单位。
    """
    if not token:
        return None, None
    payload = _jwt_payload(token)
    if not payload:
        return None, None

    acct_locale = str(payload.get("locale") or "") or None

    ou = str(payload.get("ou_code") or "").upper()
    if ou in _OU_MARKET:
        return _OU_MARKET[ou], acct_locale

    iss = str(payload.get("iss") or "")
    if iss:
        host = iss.split("//")[-1].split("/")[0]
        tld = host.rsplit(".", 1)[-1].lower()
        if tld == "cn":
            return "CN", acct_locale
        if tld == "com":
            return "NA", acct_locale
    return None, acct_locale


def from_vin(vin: str | None) -> tuple[None, str]:
    """VIN 前三位 → 生产地（仅展示）。第一个返回值恒为 None：产地不是市场。"""
    if not vin or len(vin) < 3:
        return None, ""
    return None, WMI_PLANT.get(vin[:3].upper(), "")


def from_api_host(host: str | None) -> str | None:
    if not host:
        return None
    return "CN" if ".cn" in host.lower() else None


async def detect() -> dict:
    """综合判定当前市场，返回判定依据以便界面如实展示。"""
    from ..config import settings
    from ..tesla import tokens

    signals: list[str] = []
    market = None
    acct_locale = None

    try:
        token = await tokens.access_token()
    except Exception:  # noqa: BLE001
        token = None
    region_code, acct_locale = from_token(token)
    if region_code == "CN":
        market = "CN"
        signals.append("Tesla account is registered in the China region")
    elif region_code:
        # 以前这里只记一条 signal 却不赋值 market，于是 JWT 明明说了 NA / EU
        # 也等于没读到。后果很具体：一个美国账号，只要 TeslaMate 还没反查出
        # 地址（刚装上、或 Nominatim 超时），market 就是空 → apply_market 整个
        # 跳过 → 用户停在 _DEFAULT 的「中文 + 公里 + 摄氏度 + bar」上。
        # 下面按国家细分的信号会覆盖这里，所以先认大区不会挡住 US/CA/GB 的区分。
        market = region_code           # NA / EU
        signals.append(f"Tesla account region: {region_code}")

    if market is None and from_api_host(settings.tesla_api_host) == "CN":
        market = "CN"
        signals.append("API host points at the China region")

    # 国家级细分：靠 TeslaMate 反查出来的地址国家码。
    # 这是唯一能区分美国/加拿大/英国的信号——而三者单位各不相同
    # （加拿大：英文 + 公里 + 摄氏度；英国：英里 + 摄氏度）。
    try:
        from .. import db

        row = await db.fetchrow(
            "SELECT country FROM addresses WHERE country IS NOT NULL"
            " GROUP BY country ORDER BY count(*) DESC LIMIT 1"
        )
        country = (row["country"] or "").strip() if row else ""
        code = _COUNTRY_CODE.get(country.lower())
        if code:
            market = code
            signals.append(f"Most-visited places are in {country}")
    except Exception:  # noqa: BLE001
        pass

    # VIN 只用来展示产地，**不再**用于判断市场：
    # 上海产的车大量出口欧洲/亚太，弗里蒙特产的也进口到中国，两个方向都会错。
    plant_desc = ""
    try:
        from .. import db

        row = await db.fetchrow("SELECT vin FROM cars WHERE vin IS NOT NULL LIMIT 1")
        if row and row["vin"]:
            _, plant_desc = from_vin(row["vin"])
            if plant_desc:
                signals.append(f"Built at {plant_desc} (informational — plant is not market)")
    except Exception:  # noqa: BLE001
        pass

    if market == "NA":
        market = "US"   # 大区能确定但分不出美/加时按美国，用户可手动改

    return {
        "market": market or "",
        "signals": signals,
        "plant": plant_desc,
        "account_locale": acct_locale,
        # 有 JWT 大区，或者按地址国家定下了具体国家，都算判得准。
        # 只看 region_code 的话，纯靠地址判出来的结果会被谎报成"不确定"。
        "confident": bool(region_code) or market in ("US", "CA", "GB", "CN"),
    }
