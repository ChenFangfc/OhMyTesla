"""SEI 驾驶行为分析的数学验证。

为什么用合成序列而不是真片段：`_analyse` 的输入是 sei.extract() 吐出的采样字典，
拿真片段跑只能证明"没崩"，证明不了"减速度算得对"——真片段里没有已知答案。
合成序列每一条的期望值都是手算出来的，所以它能证明数字本身。

跑法：python3 services/hub/tests/test_sei_analyse.py（无需 pytest / 无需依赖）
"""

import sys
from pathlib import Path

# index.py 里有包内相对导入，直接 import 会失败；这里只把纯函数段抠出来执行。
# 这样测试不依赖 FastAPI / aiosqlite / 数据库，任何机器上都能跑。
_SRC = (Path(__file__).resolve().parents[1] / "app" / "dashcam" / "index.py").read_text()
_NS: dict = {}
exec(_SRC[_SRC.index("HARD_BRAKE_MS2"):_SRC.index("async def index_event")], _NS)
_analyse = _NS["_analyse"]
HARD_BRAKE_MS2 = _NS["HARD_BRAKE_MS2"]
MIN_BRAKE_SPEED_KMH = _NS["MIN_BRAKE_SPEED_KMH"]


def sample(t: float, kmh: float, ap: str = "") -> dict:
    return {"t": t, "speed_kmh": kmh, "ap": ap, "lat": 40.0, "lon": -88.0}


def brakes(events):
    return [e for e in events if e["kind"] == "hard_brake"]


def disengages(events):
    return [e for e in events if e["kind"] == "disengage"]


def test_steady_speed_distance():
    """72 km/h = 20 m/s，跑 10 秒正好 200 m。"""
    agg, ev = _analyse([sample(i * 0.5, 72.0) for i in range(21)], 1000.0)
    assert abs(agg["duration_s"] - 10.0) < 1e-6
    assert abs(agg["dist_km"] - 0.2) < 1e-3
    assert abs(agg["speed_avg"] - 72.0) < 0.01
    assert ev == []


def test_hard_brake_magnitude():
    """0.5 秒里掉 10 km/h：10/3.6/0.5 = 5.56 m/s²，超过 4.0 的门槛。"""
    agg, ev = _analyse([sample(0.0, 90.0), sample(0.5, 80.0), sample(1.0, 80.0)], 2000.0)
    b = brakes(ev)
    assert len(b) == 1
    assert abs(b[0]["magnitude"] - 5.56) < 0.01
    assert b[0]["ts"] == 2000.5          # 绝对时间 = 片段起点 + 段内偏移
    assert b[0]["t_offset"] == 0.5       # 播放器靠这个跳播


def test_low_speed_braking_ignored():
    """20→10 km/h 的减速度同样是 5.56，但停车场里刹一脚不值得回看。"""
    _, ev = _analyse([sample(0.0, 20.0), sample(0.5, 10.0)], 0.0)
    assert brakes(ev) == []


def test_gentle_braking_ignored():
    """0.56 m/s² 是日常收油门的量级，不该报。"""
    _, ev = _analyse([sample(0.0, 90.0), sample(0.5, 89.0)], 0.0)
    assert brakes(ev) == []


def test_fsd_share_and_disengagement():
    """FSD 10 秒 + 人工 10 秒 → 各半，且中间算一次接管。"""
    s = [sample(i * 0.5, 60.0, "FSD") for i in range(21)]
    s += [sample(10.5 + i * 0.5, 60.0, "") for i in range(20)]
    agg, ev = _analyse(s, 0.0)
    assert abs(agg["fsd_s"] - 10.0) < 0.01
    assert abs(agg["manual_s"] - 10.0) < 0.01
    assert agg["disengage"] == 1
    d = disengages(ev)
    assert len(d) == 1 and abs(d[0]["speed_kmh"] - 60.0) < 0.01


def test_stationary_disengage_ignored():
    """停着退出 AP 不是接管——挂 P 就会退出，那是正常收车。"""
    agg, ev = _analyse([sample(0.0, 0.0, "AP"), sample(0.5, 0.0, "")], 0.0)
    assert agg["disengage"] == 0
    assert disengages(ev) == []


def test_dropped_frames_do_not_fake_a_brake():
    """采样间隔 5 秒说明中间掉帧了，拿它算减速度会凭空造出一次急刹。"""
    agg, ev = _analyse([sample(0.0, 100.0), sample(5.0, 0.0)], 0.0)
    assert brakes(ev) == []
    assert agg["duration_s"] == 0.0


def test_missing_speed_tolerated():
    """SEI 偶尔缺 speed；缺的那一步只能跳过，不能当成减速到 0。"""
    agg, ev = _analyse(
        [sample(0.0, 80.0), {"t": 0.5, "speed_kmh": None, "ap": ""}, sample(1.0, 80.0)], 0.0)
    assert brakes(ev) == []
    assert agg["dist_km"] >= 0


if __name__ == "__main__":
    failed = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ✅ {name}")
            except AssertionError as e:
                failed.append(name)
                print(f"  ❌ {name}: {e}")
    print()
    print("全部通过" if not failed else f"失败 {len(failed)} 项: {failed}")
    sys.exit(1 if failed else 0)
