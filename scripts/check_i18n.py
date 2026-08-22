#!/usr/bin/env python3
"""词表一致性检查。CI 门禁之一。

为什么值得一道门禁：i18n.js 里 t() 找不到键时会回退英文并 console.warn。
在浏览器控制台里没人看，于是漏翻的中文界面上会静悄悄地冒出英文，
反过来英文界面上冒出中文——两种都像"设计如此"，能藏很久。

查四件事：
1. 两份词表的键集合完全一致
2. 同一个键的 {占位符} 两边一致（少一个变量就会把 "{n} places" 原样显示出来）
3. 模板和 JS 里用到的键都存在
4. 词表里有没有从来没被用到的键（只警告，不拦——有些键是动态拼的）
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "services/hub/app/i18n/locales"
WEB = ROOT / "services/hub/app/web"
APP = ROOT / "services/hub/app"

# 运行时用字符串拼出来的键，静态扫不到。加前缀白名单，别误报。
DYNAMIC_PREFIXES = (
    "state.", "cmd.", "rule.", "alerts.rule.", "timeline.s_",
    "cmdk.", "unit.", "market.", "banner.", "conn.",
)


def load(name: str) -> dict:
    p = LOCALES / name
    if not p.exists():
        sys.exit(f"缺少词表 {p}")
    return json.loads(p.read_text())


def placeholders(s: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", s))


def _strip_comments(text: str) -> str:
    """去掉注释再扫。

    i18n.js 的模块头注释里写着 `t("alerts.sessions", {n: 3})` 当用法示例，
    不去注释的话它会被当成真实引用，然后报"词表缺 alerts.sessions"——
    一个永远修不掉的假警报。Jinja 的 {# #} 同理。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)     # JS 块注释
    text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)      # Jinja 注释
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)      # JS 行注释（整行的）
    return text


def used_keys() -> set[str]:
    """界面和服务端引用到的键。

    只扫 web/ 是不够的：告警文案是在 Python 里用 t(key, locale) 拼的
    （alerts/notifier.py、alerts/sentry.py、alerts/engine.py 都是），
    漏掉它们的话，推送到手机上的通知漏翻译永远查不出来。
    """
    keys: set[str] = set()
    py = [f for f in APP.rglob("*.py") if "web" not in f.parts]
    for f in list(WEB.rglob("*.html")) + list(WEB.rglob("*.js")) + py:
        if "vendor" in f.parts:
            continue
        text = _strip_comments(f.read_text(errors="ignore"))
        keys |= set(re.findall(r'data-i18n(?:-title|-html|-ph|-aria)?="([^"]+)"', text))
        keys |= set(re.findall(r'\bt\(\s*"([^"]+)"', text))
        keys |= set(re.findall(r"\bt\(\s*'([^']+)'", text))
        # Python 侧的 t(f"prefix.{var}", loc)：只能取到前缀，交给下面的过滤丢掉
        keys |= set(re.findall(r'\bt\(\s*f"([^"{]+)', text))
    # 形如 t("prefix." + x) / t("prefix_" + x) 的动态键，正则只能切到前缀部分。
    # 以 . 或 _ 结尾的一律当动态前缀扔掉——真实键名不会这么结尾。
    # 含 ${...} 的是模板字面量插值（data-i18n="${label}"），键名在运行时才定，
    # 静态扫不出来，也当动态处理。
    return {
        k for k in keys
        if k and not k.endswith((".", "_")) and "${" not in k and "{{" not in k
    }


def main() -> int:
    zh, en = load("zh.json"), load("en.json")
    errors: list[str] = []
    warnings: list[str] = []

    only_zh = sorted(set(zh) - set(en))
    only_en = sorted(set(en) - set(zh))
    if only_zh:
        errors.append(f"只有中文有的键 ({len(only_zh)}): {only_zh[:10]}")
    if only_en:
        errors.append(f"只有英文有的键 ({len(only_en)}): {only_en[:10]}")

    for k in sorted(set(zh) & set(en)):
        pz, pe = placeholders(zh[k]), placeholders(en[k])
        if pz != pe:
            errors.append(f"占位符不一致 {k}: zh={sorted(pz)} en={sorted(pe)}")

    for k, v in list(zh.items()) + list(en.items()):
        if not isinstance(v, str):
            errors.append(f"{k} 的值不是字符串: {type(v).__name__}")
        elif not v.strip():
            errors.append(f"{k} 是空串")

    used = used_keys()
    missing = sorted(k for k in used if k not in zh or k not in en)
    if missing:
        errors.append(f"界面用到但词表没有的键 ({len(missing)}): {missing[:15]}")

    unused = sorted(
        k for k in zh
        if k not in used and not k.startswith(DYNAMIC_PREFIXES)
    )
    if unused:
        warnings.append(f"词表里没被用到的键 ({len(unused)}): {unused[:10]}")

    print(f"zh {len(zh)} 键 · en {len(en)} 键 · 界面引用 {len(used)} 键")
    for w in warnings:
        print(f"  ⚠️  {w}")
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        return 1
    print("  ✅ 词表一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
