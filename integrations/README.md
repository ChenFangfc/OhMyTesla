# 现成的集成文件

这些是**可以直接导入的文件**，不是文档。照着文档一步步配置的东西，
十个人里有九个配错一处然后放弃。

| 文件 | 用法 |
|---|---|
| `home-assistant/mqtt-sensors.yaml` | 贴进 HA 的 `configuration.yaml`。**多数人不需要**——在 `.env` 里设 `HASS_DISCOVERY=true` 就会自动发现同样这批实体。 |
| `home-assistant/blueprint-sentry-alert.yaml` | 放进 HA 的 `config/blueprints/automation/ohmytesla/`，然后在「自动化 → 用蓝图创建」里选它。 |
| `node-red/sentry-to-telegram.json` | Node-RED 菜单 → 导入 → 粘贴。导入后要改两处：broker 地址、把末尾的 debug 节点换成你的通知节点。 |

## 数据从哪来

OhmyTesla 会把**它自己算出来的**东西回发到同一个 mosquitto：

| 主题 | 内容 |
|---|---|
| `teslamate/cars/<id>/sentry_event` | 哨兵事件（JSON，含 level / title / body / geofence） |
| `ohmytesla/sentry_state` | `active` / `idle` |
| `ohmytesla/data_stale` | `ON` / `OFF`——采集是否中断 |
| `ohmytesla/vampire_kwh_day` | 停放耗电 kWh/天 |
| `ohmytesla/sleep_score` | 睡眠评分 % |
| `ohmytesla/logging_coverage` | 采集覆盖率 % |
| `ohmytesla/fsd_share` | 自驾占比 % |
| `ohmytesla/maintenance_due` | 待办保养项数 |

车辆本身的电量、位置、门窗、胎压等约 80 个主题由 **TeslaMate 自己**发布，
不在这张表里 —— 那部分的自动发现从 TeslaMate v4.1.0 起也是它自带的。
