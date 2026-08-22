"""TeslaMateApi 兼容层的契约测试。

这一层存在的唯一理由是让 MateDroid / T-Buddy 这类**已经写好的** App 能直接
连过来。它们按 teslamateapi v1.25.0 的 JSON 形状取值，少一个字段就是界面上
一个空白，而且不会报错 —— 用户只会觉得"这个 App 坏了"。

所以字段名不是实现细节，是对外契约。下面这份期望是从 teslamateapi 的 Go
结构体 json tag 逐字抄下来的，改动任何一个都必须是有意识的决定。

跑法（需要服务在跑）：
  OMT_URL=http://localhost:8800 OMT_PW=... python3 services/hub/tests/test_teslamateapi_contract.py
CI 里由 run.sh 传入。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("OMT_URL", "http://localhost:8800").rstrip("/")
PW = os.environ.get("OMT_PW", "")
CAR = os.environ.get("OMT_CAR", "1")

# 路径 → 该层必须存在的字段。'[]' 表示取数组第一个元素。
CONTRACT = {
    "v1/cars": {
        "data.cars[].": ["car_id", "name", "car_details", "car_exterior",
                         "car_settings", "teslamate_details", "teslamate_stats"],
        "data.cars[].car_details.": ["eid", "vid", "vin", "model", "trim_badging", "efficiency"],
        "data.cars[].car_exterior.": ["exterior_color", "spoiler_type", "wheel_type"],
        "data.cars[].car_settings.": ["suspend_min", "suspend_after_idle_min",
                                      "req_not_unlocked", "free_supercharging",
                                      "use_streaming_api"],
        "data.cars[].teslamate_stats.": ["total_charges", "total_drives", "total_updates"],
    },
    f"v1/cars/{CAR}/status": {
        "data.": ["car", "status", "units"],
        "data.car.": ["car_id", "car_name"],
        "data.units.": ["unit_of_length", "unit_of_temperature", "unit_of_pressure"],
        "data.status.": ["display_name", "state", "state_since", "odometer", "car_status",
                         "car_details", "car_exterior", "car_geodata", "car_versions",
                         "driving_details", "climate_details", "battery_details",
                         "charging_details", "tpms_details"],
        "data.status.car_status.": ["healthy", "locked", "sentry_mode", "windows_open",
                                    "doors_open", "driver_front_door_open",
                                    "driver_rear_door_open", "passenger_front_door_open",
                                    "passenger_rear_door_open", "trunk_open", "frunk_open",
                                    "is_user_present", "center_display_state"],
        "data.status.battery_details.": ["est_battery_range", "rated_battery_range",
                                         "ideal_battery_range", "battery_level",
                                         "usable_battery_level"],
        "data.status.charging_details.": ["plugged_in", "charging_state", "charge_energy_added",
                                          "charge_limit_soc", "charge_port_door_open",
                                          "charger_actual_current", "charger_phases",
                                          "charger_power", "charger_voltage",
                                          "charge_current_request", "charge_current_request_max",
                                          "scheduled_charging_start_time", "time_to_full_charge"],
        "data.status.tpms_details.": ["tpms_pressure_fl", "tpms_pressure_fr",
                                      "tpms_pressure_rl", "tpms_pressure_rr",
                                      "tpms_soft_warning_fl", "tpms_soft_warning_fr",
                                      "tpms_soft_warning_rl", "tpms_soft_warning_rr"],
    },
    f"v1/cars/{CAR}/battery-health": {
        "data.": ["car", "battery_health", "units"],
        "data.battery_health.": ["max_range", "current_range", "max_capacity",
                                 "current_capacity", "rated_efficiency",
                                 "battery_health_percentage"],
    },
    f"v1/cars/{CAR}/drives?limit=1": {
        "data.": ["car", "drives", "units"],
        "data.drives[].": ["drive_id", "start_date", "end_date", "start_address", "end_address",
                           "odometer_details", "duration_min", "duration_str", "speed_max",
                           "speed_avg", "power_max", "power_min", "battery_details",
                           "range_ideal", "range_rated", "outside_temp_avg", "inside_temp_avg",
                           "energy_consumed_net", "consumption_net"],
        "data.drives[].odometer_details.": ["odometer_start", "odometer_end", "odometer_distance"],
        "data.drives[].battery_details.": ["start_usable_battery_level", "start_battery_level",
                                           "end_usable_battery_level", "end_battery_level",
                                           "reduced_range", "is_sufficiently_precise"],
        "data.drives[].range_rated.": ["start_range", "end_range", "range_diff"],
    },
    f"v1/cars/{CAR}/charges?limit=1": {
        "data.": ["car", "charges", "units"],
        "data.charges[].": ["charge_id", "start_date", "end_date", "address",
                            "charge_energy_added", "charge_energy_used", "cost",
                            "duration_min", "duration_str", "battery_details", "range_ideal",
                            "range_rated", "outside_temp_avg", "odometer",
                            "latitude", "longitude"],
    },
    f"v1/cars/{CAR}/updates": {
        "data.": ["car", "updates"],
        "data.updates[].": ["update_id", "start_date", "end_date", "version"],
    },
    "v1/globalsettings": {"data.": ["settings"]},
}


def fetch(path: str):
    req = urllib.request.Request(f"{URL}/api/{path}")
    if PW:
        token = base64.b64encode(f"ohmytesla:{PW}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, json.loads(r.read())


def dig(obj, path: str):
    for part in [p for p in path.split(".") if p]:
        if part.endswith("[]"):
            obj = obj.get(part[:-2])
            if not obj:
                return None
            obj = obj[0]
        else:
            if not isinstance(obj, dict) or part not in obj:
                return None
            obj = obj[part]
    return obj


def main() -> int:
    fails: list[str] = []
    for path, checks in CONTRACT.items():
        try:
            status, body = fetch(path)
        except urllib.error.HTTPError as e:
            print(f"  ❌ {path} → HTTP {e.code}")
            fails.append(path)
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {path} → {e}")
            fails.append(path)
            continue
        if status != 200:
            print(f"  ❌ {path} → HTTP {status}")
            fails.append(path)
            continue
        for where, want in checks.items():
            got = dig(body, where)
            if got is None:
                # 没有数据（比如库里一次充电都没有）不算契约违约，但要说出来
                print(f"  ⚠️  {path} {where} 无数据可校验")
                continue
            missing = [k for k in want if k not in got]
            if missing:
                print(f"  ❌ {path} {where} 缺字段 {missing}")
                fails.append(f"{path}{where}")
            else:
                print(f"  ✅ {path} {where}")

    # 指令端点必须明确 501，不能假装成功
    for path in (f"v1/cars/{CAR}/wake_up",):
        req = urllib.request.Request(f"{URL}/api/{path}", method="POST", data=b"")
        if PW:
            req.add_header("Authorization",
                           "Basic " + base64.b64encode(f"ohmytesla:{PW}".encode()).decode())
        req.add_header("Sec-Fetch-Site", "same-origin")
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"  ❌ {path} 应当返回 501")
            fails.append(path)
        except urllib.error.HTTPError as e:
            ok = e.code == 501
            print(f"  {'✅' if ok else '❌'} {path} → {e.code}（期望 501）")
            if not ok:
                fails.append(path)

    print()
    print("契约完全匹配" if not fails else f"失败 {len(fails)} 项: {fails[:5]}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
