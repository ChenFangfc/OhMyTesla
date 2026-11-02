from pathlib import Path

from pydantic import model_validator
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
    # 统计分桶用的时区。TeslaMate 的时间列是 naive UTC，按 UTC 分桶会让
    # 东八区凌晨的行程落到前一天。空 = 跟随容器的 TZ 环境变量，再没有就 UTC。
    tz: str = ""

    # TeslaMateApi 兼容层的 API token。留空 = 只靠全站 Basic 认证；
    # 设了之后 /api/v1/* 还要额外带 Bearer 或 X-Api-Token。
    # 用途是分权：Basic 密码给家人，App 用的 token 单独一把、可单独吊销。
    teslamateapi_token: str = ""

    # 允许自动化规则调用解锁/开厢这类高危指令。默认关闭 ——
    # 一条写错的规则半夜把车解锁，和其它任何 bug 都不是一个量级的后果。
    automation_allow_unlock: bool = False

    # 允许 AI 通过 MCP 做写操作（控车、打标签、建自动化、静音告警）。
    # 默认关闭，关闭时这些工具连列表都不出现 —— 模型看不见就不会去试。
    # 打开后高风险动作仍需两段式确认，见 mcp/write_tools.py。
    mcp_write: bool = False

    # 关联服务（用于门户跳转链接）
    teslamate_url: str = "http://teslamate:4000"
    grafana_url: str = "http://grafana:3000"
    teslamate_public_port: int = 4000
    teslamate_bind_addr: str = ""   # compose 传进来；127.0.0.1 = 只有服务器本机打得开
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
    tesla_access_token: str = ""      # 手动指定 token（可选，优先级低于界面填写）
    # 车辆指令设置向导的预填值（界面里保存过就以界面为准）
    tesla_fleet_domain: str = ""        # 放公钥的 https 域名，例如 yourname.github.io
    tesla_fleet_redirect_uri: str = ""  # 特斯拉登录后跳回的地址
    tesla_fleet_region: str = ""        # na / eu / cn

    # 充电成本：把算出的费用回写 TeslaMate（Grafana 面板也能看到）
    cost_write_back: bool = False

    # ABRP（A Better Routeplanner）实时遥测中继：把真实电量/车速/位置喂给它，
    # 路线规划就按你这台车的实际状态算，而不是车型平均模型。
    abrp_enabled: bool = False
    abrp_token: str = ""          # ABRP 里「Live Data」生成的 user token
    abrp_api_key: str = "32b2162f-9599-4647-8139-66e9f9528370"  # 社区通用 key
    abrp_skip_location: bool = False   # 只发电量不发经纬度
    # 数据多旧就不再往 ABRP 发（秒）。live.cars 是"最后已知状态"，
    # 采集停了它也不会变——照发等于给 ABRP 一个盖着当前时间戳的陈旧位置。
    abrp_max_age_seconds: int = 900

    # Home Assistant MQTT 自动发现。只发 OhmyTesla 自己算出来的实体
    # （哨兵会话、采集中断、停放耗电、待办保养）——车辆本身的那些
    # TeslaMate v4.1.0 已经自带发现，重复发会在 HA 里出现两套实体。
    hass_discovery: bool = False

    # 哨兵活动会话：连续活动合并窗口 / 升级阈值（秒）
    sentry_dedup_seconds: int = 15
    sentry_escalate_seconds: int = 90
    # 轮询层（center_display_state=7）总开关；有 Fleet 实时流时自动让位
    sentry_polled_alerts: bool = True
    # Fast sentry check: while a car is awake with Sentry armed, read its
    # vehicle_state this often (seconds) instead of waiting for TeslaMate's
    # 60 s poll. 0 = off. Overridable in Settings. 5 s is the rate TeslaMate
    # itself polls a charging car at (2.5 s while driving); Tesla documents 60
    # data requests a minute per vehicle.
    sentry_fast_poll_seconds: int = 5
    # 保留期（天）：告警历史 / 哨兵会话 / 合成任务超过这个天数就清掉。
    # 0 = 永久保留。这三张表原本只增不删，而它们和电价、token 共用同一个
    # SQLite 文件，写入还是串行的 —— 跑上几年会拖慢每一次写。
    history_retention_days: int = 180
    # 导出成品 / 转码预览 / 缩略图缓存的保留期（天）。0 = 不清理。
    # 一个 6 机位事件合出来几百 MB，不清会把磁盘吃光。
    media_retention_days: int = 30

    # 多久收不到车辆数据就判定"采集中断"并严重告警（秒）。
    # TeslaMate 停车+哨兵时 30 秒一轮，休眠时最长十几分钟一次，故默认 30 分钟。
    data_stale_seconds: int = 1800

    @model_validator(mode="before")
    @classmethod
    def _drop_env_comments(cls, data):
        """`KEY=   # note` in .env reaches the container as the note itself:
        compose keeps the comment when the value is empty (seen live with
        ALERT_QUIET_HOURS). A comment is prose, so a value that starts with
        '#' and has a space in it is treated as unset; a password such as
        '#x9!' is left alone."""
        if isinstance(data, dict):
            return {k: v for k, v in data.items()
                    if not (isinstance(v, str) and v.lstrip().startswith("#") and " " in v.strip())}
        return data

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
