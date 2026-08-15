# OhmyTesla 架构设计

## 核心原则：编排，不 fork

TeslaMate 以官方 Docker 镜像原样运行（`teslamate/teslamate:latest`），可随上游随时升级。
OhmyTesla 的全部新功能位于独立的 **Hub 服务**（`services/hub`，Python/FastAPI），通过
TeslaMate 生态公认的三条"缝"接入：

1. **MQTT 订阅**（`teslamate/cars/{car_id}/+`）—— 实时状态（唯一能拿到哨兵开关、实时位置流的通道；这些不落库）；
2. **Postgres 只读**—— 历史数据（positions/drives/charging_processes/charges/updates/geofences/addresses）；
3. **文件系统**—— 挂载的 TeslaCam 素材目录（U 盘拷贝或 teslausb 自动同步）。

Hub 自己的状态（告警历史、规则开关、合成任务）存独立 SQLite（`data/hub/hub.db`），
不向 TeslaMate 库写任何东西，升级永不冲突。

```
┌────────────┐   Tesla API    ┌────────────┐
│  Tesla 云   │───────────────▶│  TeslaMate │──── MQTT (mosquitto) ────┐
└────────────┘                └─────┬──────┘                          │
                                    │ Postgres                        ▼
                              ┌─────▼──────┐                   ┌──────────────┐
                              │  postgres  │◀──── 只读 ────────│ OhmyTesla Hub│
                              └─────┬──────┘                   │  (FastAPI)   │
                              ┌─────▼──────┐                   │ ┌──────────┐ │
                              │  Grafana   │◀── 深度面板外链 ───│ │告警引擎  │─┼─▶ SMTP/ntfy/TG/Webhook
                              └────────────┘                   │ │合成管线  │─┼─▶ ffmpeg → mp4
                                  TeslaCam 素材 ── 挂载 ───────▶│ │SEI 解码  │ │
                                                               │ │洞察查询  │ │
                                                               └─┴──────────┴─┘
```

## 模块设计

### livestate —— MQTT 实时层
- 订阅 `teslamate/cars/#`，维护每车最新键值快照；
- 变化（transition）分发给：告警引擎钩子、SSE 订阅者（门户实时刷新）；
- 启动时 retained 消息回放阶段（old=None）不触发告警。

### alerts —— 告警引擎
- 规则以 transition 驱动：哨兵开启且无人时的开门/开窗/开厢/解锁 → critical；
  无人状态下车辆开始移动（疑似盗窃）→ critical；充电中断/胎压/低电量/固件更新 → warning/info；
- 静默时段只放行 critical；每（车,规则）冷却防轰炸；规则可界面开关（SQLite）；
- 通知经 Apprise 统一发送（SMTP/ntfy/Telegram + 100+ 服务），另有原生 Webhook POST。
- 已知边界：TeslaMate 不转发车辆报警器状态，纯"哨兵检测到可疑人员"级别的事件需
  Fleet Telemetry（`SentryMode` Aware/Panic 字段）——见路线图 Tier 2。

### dashcam —— 行车记录仪工作室
- **scanner**：解析 TeslaCam 目录（SentryClips/SavedClips/RecentClips），event.json + thumb + 分段/机位聚合；RecentClips 按 90 秒断档分组为伪事件；
- **sei**：独立实现的 SEI 提取器（mdat NAL 扫描 + 手写 protobuf wire 解码，H.264/HEVC 均可），输出档位/车速/方向盘/踏板/转向灯/AP/GPS/加速度逐帧序列；停车片段与 2026.20+ 加密片段自然返回空；
- **telemetry**：TeslaCam 文件名（车机本地时间, `TESLACAM_TZ`）↔ TeslaMate `positions.date`（UTC 微秒）对齐；不规则采样线性插值到固定步长；生成 ASS 字幕叠加层（libass 内置渲染，Noto CJK 中文无忧）：底部数据条（速度/功率/电量/续航/海拔/外温/里程/时钟/GPS）+ 左上 SEI 仪表簇（档位/AP/转向灯/方向盘/踏板/G 值）；
- **composer**：ffmpeg filtergraph 构建——每机位 concat → trim → scale/pad/fps 归一 → xstack/overlay 布局（grid / front_pip / back_pip）→ ass 叠加 → libx264；只用"所有选中机位齐备"的分段保证时间轴对齐；
- **jobs**：单 worker 串行队列，`-progress` 管道解析进度，支持取消，输出至 `data/hub/exports/`。

### insights —— 数据洞察
- 电池健康：① 充电能量法（ΔSOC≥25 的充电段推算容量散点）② 额定续航法（按月满电等效续航趋势 × cars.efficiency 折算 kWh），给出当前容量与衰减百分比；
- 充电：月度 AC/DC 堆叠、常用地点、最近记录；行程：月度里程、能耗-气温曲线、最近行程；
- 胎压最新值 + 30 天趋势；车辆记录（最速/最大功率/最强回收/最长行程/最高海拔等）；固件更新史。

### web —— 门户
- Jinja2 + 原生 JS（零构建链），SSE 实时刷新；自研迷你 SVG 图表库（十字线悬浮、图例、堆叠柱），
  配色采用经过验证的亮/暗双模式数据可视化调色板；
- 播放器：多 `<video>` 同步（主画面 + 画中画点击换位），全事件时间轴，HUD 双源遥测（SEI 逐段懒加载 + TeslaMate 整段预取），合成任务下发与进度轮询。

## 关键决策记录

| 决策 | 理由 |
|---|---|
| 单一 Hub 容器而非多微服务 | 自托管用户友好：一个新容器带来全部功能；内部模块化保留拆分余地 |
| Python + FastAPI | ffmpeg 编排、SEI 二进制解析、asyncpg/aiomqtt 生态均顺手；社区易参与 |
| ASS 字幕做视频叠加 | 单次滤镜、性能远好于逐帧 drawtext、CJK 渲染由 libass+Noto 解决 |
| 不 vendor teslamotors/dashcam 代码 | 该仓库无 LICENSE 文件；schema 字段为事实信息，解码为独立实现 |
| AGPL-3.0 | 与 TeslaMate 相同，消除其 README 主张的传染争议，也允许未来吸纳同类 AGPL 项目逻辑 |
| SQLite 而非共库建表 | TeslaMate 迁移自动执行，共库有升级碰撞风险 |

## Phase 2 已落地（2026-08-05 第二轮）

- **U 盘直读模式**（`/dashcam/usb`）：浏览器端目录扫描 + blob 本地播放 + 浏览器内 SEI 解码（`static/js/sei.js`，与 Python 版同构）+ 按事件选择性上传导入（`POST /api/dashcam/import`，路径白名单 + 容量/磁盘校验）；
- **录像监视器**（`dashcam/watcher.py`）：60s 轮询新哨兵/保存事件，告警附缩略图；基线持久化（重启窗口期同步的事件不漏报），瞬时空扫描防护，多事件合并单条告警；
- **时钟自动校正**（`dashcam/align.py`）：SEI 速度 × positions 速度滑动互相关（±120s 粗扫 + 0.25s 细化 + 重叠数归一化），偏移持久化按车存储，全链路自动应用；
- **行程页**（`/drives`）：vendored Leaflet 轨迹地图（瓦片源可配 `TILE_URL`）+ 行程时间段自动匹配录像跳播（seek 映射到分段视频时间轴）；
- **导出轨迹小窗**：ASS 矢量点阵路径 + 移动位置点，纯 libass 渲染零依赖；
- **HEVC 转码预览**：播放失败自动请求服务端 H.264 预览（全局并发限 2、缓存、唯一临时文件）；
- **Fleet Telemetry**：compose `--profile fleet` + 配置模板 + `telemetry/{VIN}/v/SentryMode` 解析 → Aware/Panic 告警（首条消息即 Panic 也不漏）；
- **门户密码**（`OHMYTESLA_PASSWORD`，Basic Auth，bytes 比较兼容非 ASCII）；哨兵事件回发 MQTT `teslamate/cars/{id}/sentry_event`。

## 剩余已知局限（诚实清单）

- 多车支持：播放器叠加/校准默认取第一辆车；多车账号校准会要求显式 `car_id`（422 提示），播放器车辆选择器待加；
- 加密片段（2026.20+ 默认开）无法解析，只能引导关闭加密；dashcam.tesla.com 的账号密钥本地解密流程逆向实现有法律与稳定性风险，不做；
- Web Push（PWA 离线推送）未实现——iOS 需 16.4+ 且加主屏；当前推荐 ntfy/Telegram；
- 充电成本电价方案（对标 TeslaMateAgile）与 Tesla 指令控制（鸣笛/闪灯/空调）未做，见 ROADMAP Phase 3。

## 双语与单位（2026-08-07）

**中美两区功能上没有差异**——同一个 API、同一套响应字段，调研未发现任何字段在中国区缺失。
真正的差异只有 6 处，且都是环境而非功能：

| 差异 | 处理方式 |
|---|---|
| OSM 瓦片在中国打不开 | `/api/config` 按地区给瓦片源（国内默认高德栅格） |
| 国内地图用 GCJ-02 基准 | `static/js/geo.js` **渲染时**偏移；绝不入库时转（会永久污染地理围栏且无法回退） |
| Nominatim 在中国超时 → 地址为空 | 已 LEFT JOIN 优雅降级，显示 "?" |
| 推送里的地图链接 | 按**地区**选（国内高德 / 其余 Google），不是按语言 |
| `remote_boombox` 中国区 403 | 本来就没做，保持 |
| 货币默认 | 电价方案里可改 |

**语言与单位彻底解耦**（`app/i18n/`）：
- `prefs.py` 存偏好；`units.py` 是换算的唯一真源（含 `temp_delta`——温差只乘比例不加偏移）
- `region.py` 检测顺序：JWT 的 `ou_code` → 接口主机 → **地址国家**（唯一能分出美/加/英的信号）
  → VIN 仅展示产地（上海产的车大量出口，用它判市场两个方向都会错）
- 市场只决定**单位**，不决定语言——在北美的中文车主要的是中文界面 + 英里
- 数据层永远公制，换算只发生在展示层：网页、推送文案、**烧进视频的叠加层**

⚠️ 视频叠加层是全系统唯一**不可逆**的地方：ASS 压进 H.264 后改不了，
而原始片段可能几天后被车覆盖。导出任务会记录当时用的语言和单位。
