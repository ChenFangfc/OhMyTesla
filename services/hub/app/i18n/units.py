"""单位换算的唯一真源。

之前换算逻辑在 Python 和 JS 里各写了一份，已经出现过分歧
（Python 版的 bar() 忽略了 digits 参数）。现在 Python 侧统一走这里，
JS 侧用同一组常数和精度表，并由 tools/i18n_lint.py 的测试向量交叉校验。

数据层永远是公制（TeslaMate 库里就是 km / °C / kW / bar），
换算只发生在"要给人看"的那一刻：网页、推送文案、烧进视频的叠加层。
"""

KM_PER_MI = 1.609344          # 精确值，不要用 0.621371 的截断倒数
BAR_PER_PSI = 0.0689475729


def dist(km, prefs, digits: int = 1):
    """距离 km → (数值, 单位名)"""
    if km is None:
        return None, _len_label(prefs)
    v = float(km) / KM_PER_MI if prefs.get("length") == "mi" else float(km)
    return round(v, digits), _len_label(prefs)


def speed(kmh, prefs, digits: int = 0):
    """速度 km/h → (数值, 单位名)。速度单位跟随距离单位，与特斯拉车机一致。"""
    if kmh is None:
        return None, _speed_label(prefs)
    v = float(kmh) / KM_PER_MI if prefs.get("length") == "mi" else float(kmh)
    return round(v, digits), _speed_label(prefs)


def temp(c, prefs, digits: int = 1):
    """温度 °C → (数值, 单位名)。绝对温度，含 +32 偏移。"""
    if c is None:
        return None, _temp_label(prefs)
    v = float(c) * 9 / 5 + 32 if prefs.get("temp") == "F" else float(c)
    return round(v, digits), _temp_label(prefs)


def temp_delta(dc, prefs, digits: int = 1):
    """温度**差** → (数值, 单位名)。

    温差只乘比例、不加偏移。用绝对公式换算温差会把"车内升温 3°C"
    变成"37.4°F"而不是正确的"5.4°F"——这个 bug 安静且看起来很合理，
    所以在第一个调用方出现之前就先把函数放在这里。
    """
    if dc is None:
        return None, _temp_label(prefs)
    v = float(dc) * 9 / 5 if prefs.get("temp") == "F" else float(dc)
    return round(v, digits), _temp_label(prefs)


def press(bar_val, prefs, digits: int = 1):
    """胎压 bar → (数值, 单位名)"""
    if bar_val is None:
        return None, _press_label(prefs)
    u = prefs.get("pressure", "bar")
    if u == "psi":
        return round(float(bar_val) / BAR_PER_PSI, digits), "psi"
    if u == "kpa":
        return round(float(bar_val) * 100, 0), "kPa"
    return round(float(bar_val), 2), "bar"


def cons(wh_km, prefs, digits: int = 0):
    """能耗 Wh/km → (数值, 单位名)。英里制是 Wh/mi，数值更大。"""
    if wh_km is None:
        return None, _cons_label(prefs)
    v = float(wh_km) * KM_PER_MI if prefs.get("length") == "mi" else float(wh_km)
    return round(v, digits), _cons_label(prefs)


# ---------- 单位名 ----------

def _len_label(p) -> str:
    return "mi" if p.get("length") == "mi" else "km"


def _speed_label(p) -> str:
    return "mph" if p.get("length") == "mi" else "km/h"


def _temp_label(p) -> str:
    return "°F" if p.get("temp") == "F" else "°C"


def _press_label(p) -> str:
    return {"psi": "psi", "kpa": "kPa"}.get(p.get("pressure", "bar"), "bar")


def _cons_label(p) -> str:
    return "Wh/mi" if p.get("length") == "mi" else "Wh/km"


def fmt(value_unit: tuple, dash: str = "–") -> str:
    """(数值, 单位) → "12.3 km"；数值为 None 时给占位符。"""
    v, u = value_unit
    return dash if v is None else f"{v} {u}"
