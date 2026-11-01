"""ffmpeg / ffprobe 的公共小件：可执行文件定位、滤镜能力探测、转义。

可执行文件：优先 FFMPEG_BIN / FFPROBE_BIN 环境变量，其次 PATH，再其次
Homebrew 的两个默认前缀。从 Finder 启动的 macOS 应用拿到的 PATH 里没有
/opt/homebrew/bin，只认 PATH 的话"装了 ffmpeg 但导出全失败"。
"""

import functools
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)

_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")


@functools.cache
def _find(name: str, env: str) -> str:
    explicit = os.environ.get(env, "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    hit = shutil.which(name)
    if hit:
        return hit
    for d in _FALLBACK_DIRS:
        p = os.path.join(d, name)
        if os.access(p, os.X_OK):
            return p
    return name  # 找不到也返回裸名：报错信息里至少能看出缺的是谁


def ffmpeg() -> str:
    return _find("ffmpeg", "FFMPEG_BIN")


def ffprobe() -> str:
    return _find("ffprobe", "FFPROBE_BIN")


def available() -> bool:
    return os.path.isabs(ffmpeg()) or shutil.which(ffmpeg()) is not None


@functools.cache
def _filters() -> frozenset[str]:
    try:
        r = subprocess.run([ffmpeg(), "-hide_banner", "-filters"],  # noqa: S603 - 参数列表、不过 shell
                           capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        log.warning("cannot list ffmpeg filters", exc_info=True)
        return frozenset()
    names = set()
    for line in r.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        # 格式: " T.C ass               V->V       Render ASS subtitles..."
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


def has_filter(name: str) -> bool:
    """这个 ffmpeg 编进了某个滤镜没有。

    Homebrew 的 ffmpeg 9 默认不带 libass，于是 `ass=` 滤镜不存在 ——
    不先问一句的话，默认开着叠加层的每一次导出都会以 "Filter not found" 失败。
    """
    return name in _filters()


def reset_caches() -> None:
    """测试用：换了 PATH / 环境变量之后重新探测。"""
    _find.cache_clear()
    _filters.cache_clear()


def escape_filter_value(value: str) -> str:
    """把任意字符串变成 filtergraph 里一个**选项值**的安全写法。

    ffmpeg 解析 `-filter_complex` 要经过两层：先按 filtergraph 语法切分
    （`[ ] , ;` 和引号、反斜杠有特殊含义），再把每个滤镜的参数按选项语法
    切分（`:` 分隔选项，引号、反斜杠再解一次）。只转义一层的话，路径里的
    `:` 或 `'` 会把 ass=filename=... 切坏。

    做法照 ffmpeg 文档「Notes on filtergraph escaping」：第一层把值整体放进
    单引号（单引号本身写成 '\\''），第二层再对整个结果转义图语法的特殊字符。
    """
    # 第一层（选项值）：单引号包起来，内部的单引号用 '\'' 断开再接上
    level1 = "'" + value.replace("'", "'\\''") + "'"
    # 第二层（filtergraph）：反斜杠、单引号和图语法字符都要再转义一次
    out = []
    for ch in level1:
        if ch in "\\'[],;":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def concat_quote(path: str) -> str:
    """concat demuxer 列表文件里的 file 指令值：单引号包起来，内部 ' 写成 '\\''。"""
    return "'" + path.replace("'", "'\\''") + "'"
