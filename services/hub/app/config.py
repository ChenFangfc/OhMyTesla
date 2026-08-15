from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # TeslaMate Postgres（只读使用）
    tm_db_host: str = "database"
    tm_db_port: int = 5432
    tm_db_user: str = "teslamate"
    tm_db_pass: str = ""
    tm_db_name: str = "teslamate"

    # MQTT
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_namespace: str = ""  # 对应 TeslaMate 的 MQTT_NAMESPACE，通常为空

    # 路径
    teslacam_root: str = "/teslacam"
    teslacam_tz: str = ""  # TeslaCam 文件名所用时区，空 = 系统时区
    data_dir: str = "/data"

    # 关联服务（用于门户跳转链接）
    teslamate_url: str = "http://teslamate:4000"
    grafana_url: str = "http://grafana:3000"
    teslamate_public_port: int = 4000
    # 推送里给的链接要能在手机上点开。留空时只能给相对路径，
    # 手机上等于没有链接 —— 所以这项值得单独配。
    public_url: str = ""
    grafana_public_port: int = 3000

    # 通知渠道
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    ntfy_url: str = ""
    ntfy_topic: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_webhook_url: str = ""
    apprise_urls: str = ""

    # 告警行为
    alert_quiet_hours: str = ""  # "23:00-07:00"
    alert_cooldown_seconds: int = 300
    alert_lang: str = "zh"

    # 门户访问密码（留空 = 不启用；用户名固定 ohmytesla）
    ohmytesla_password: str = ""

    # 地图瓦片源（留空按地区自动选：中国区用国内源，其余用 OSM）
    tile_url: str = ""
    # 瓦片坐标基准：wgs84 | gcj02。国内地图源必须填 gcj02，
    # 否则轨迹会整体偏移 50-500 米（数据库存的是 WGS-84）
    tile_datum: str = ""

    # Fleet Telemetry MQTT 主题前缀（官方 mqtt dispatcher 的 topic_base）
    fleet_topic_prefix: str = "telemetry"
    # 车辆重连后会重放缓冲消息，这段时间内的哨兵状态不当作新事件
    fleet_replay_gate_seconds: int = 60

    # 托管遥测中转桥接（无需公网 IP）
    fleet_bridge_enabled: bool = False
    fleet_bridge_provider: str = "teslemetry"   # teslemetry | tessie | myteslamate | custom
    fleet_bridge_token: str = ""
    fleet_bridge_url: str = ""          # 留空用 provider 预设；支持 {vin} 占位
    fleet_bridge_transport: str = ""    # sse | ws，留空用预设
    fleet_bridge_auth_mode: str = ""    # header | query | none
    fleet_bridge_auth_name: str = ""
    fleet_bridge_auth_prefix: str = ""
    fleet_bridge_vins: str = ""         # 逗号分隔；留空则读 TeslaMate 的车辆表
    fleet_bridge_subscribe: str = ""    # 仅 ws：订阅消息模板，支持 {vin}/{token}
    fleet_bridge_ready_prefix: str = ""  # 仅 ws：确认订阅生效的字符串
    fleet_bridge_stale_seconds: int = 120
    fleet_bridge_idle_timeout: int = 45
    fleet_bridge_backoff_max: int = 600

    # 车辆指令：与 TeslaMate 共用的 ENCRYPTION_KEY（可自动复用其 token）
    encryption_key: str = ""
    tesla_api_host: str = "https://owner-api.teslamotors.com"
    tesla_command_proxy: str = ""     # vehicle-command 签名代理地址（2021+ 车型需要）
    tesla_access_token: str = ""      # 手动指定 token（可选，优先级低于界面填写）

    # 充电成本：把算出的费用回写 TeslaMate（Grafana 面板也能看到）
    cost_write_back: bool = False

    # 哨兵活动会话：连续活动合并窗口 / 升级阈值（秒）
    sentry_dedup_seconds: int = 15
    sentry_escalate_seconds: int = 90
    # 轮询层（center_display_state=7）总开关；有 Fleet 实时流时自动让位
    sentry_polled_alerts: bool = True
    # 多久收不到车辆数据就判定"采集中断"并严重告警（秒）。
    # TeslaMate 停车+哨兵时 30 秒一轮，休眠时最长十几分钟一次，故默认 30 分钟。
    data_stale_seconds: int = 1800

    @property
    def preview_dir(self) -> Path:
        p = Path(self.data_dir) / "preview"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def exports_dir(self) -> Path:
        p = Path(self.data_dir) / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def sqlite_path(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p / "hub.db"


settings = Settings()
