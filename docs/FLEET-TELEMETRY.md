# Fleet Telemetry 接入指南（哨兵真实事件流）

TeslaMate 的 MQTT 只能推断"哨兵开着时门被打开"这类强信号。要拿到**哨兵本身的
Aware（检测到可疑活动）/ Panic（警报拉响）事件**，需要接入特斯拉官方
[Fleet Telemetry](https://github.com/teslamotors/fleet-telemetry) 流式遥测。
OhmyTesla 已内置解析：只要遥测流经 MQTT 进入本套件，`哨兵注意` / `哨兵警报拉响`
两条告警规则即自动生效。

## 先选路线

| 路线 | 你要做的 | 费用 |
|---|---|---|
| 不接 | 什么都不用做：哨兵开启时每 5 秒查一次车，通常 5 秒左右推到手机 | 免费 |
| 付费中转（最省事） | 在 Teslemetry 等服务里登录 Tesla 账号、批准它的钥匙，把令牌填进 `.env`（见下文「路线 A+」） | 中转商的订阅费 |
| 自建 | 自己的 Tesla 开发者应用（和远程控制是同一个）、开发者账号绑卡、公网 IP + 域名 + 证书、路由器转发端口（见「路线 B」） | Tesla 每月 $10 额度内免费 |

接上以后通常 1–3 秒到达，信号差的停车场会更久。

## 成本

流式计费约 **每 1 美元 15 万条信号**，SentryMode 只在变化时才发送——一个专注哨兵的字段
配置**完全可以跑在特斯拉每月 $10 的免费额度内**（对比 SentryPro $5.99/月、Tessie
$12.99/月订阅）。轮询 Fleet API 反而是成本陷阱（约 $86/月 @1 分钟间隔）；本套件的快速检查走的是不收费的 Owner API，不走 Fleet API。

## 路线 A：MyTeslaMate 等托管代理（简单）

第三方代理（如 MyTeslaMate）替你完成车辆遥测接入，再把数据转发到你的 MQTT。
只需把它的转发主题前缀配置为 `FLEET_TOPIC_PREFIX`（默认 `telemetry`）即可。
注意各代理的收费政策会变化，接入前确认现状。

## 路线 B：完全自托管（主权路线）

前置条件（一次性）：

1. **Tesla 开发者账号**：developer.tesla.com 注册应用（中国大陆使用 developer.tesla.cn）；
2. **公网域名 + TLS**：fleet-telemetry 服务器必须有 FQDN 和 CA 签发的证书（mTLS）；
3. **托管公钥**：把应用公钥放到 `https://你的域名/.well-known/appspecific/com.tesla.3p.public-key.pem`；
4. **虚拟钥匙**：车辆通过 Tesla App 配对你的应用虚拟钥匙（MCU1 不支持；2021 前 S/X 有豁免规则）；
5. **车辆固件** 2023.20+。

配置步骤：

```bash
cp config/fleet-telemetry.example.json config/fleet-telemetry.json
# 把你的证书放进 config/certs/server.crt / server.key
docker compose --profile fleet up -d
```

然后用 Fleet API 给车辆下发遥测字段配置（fleet_telemetry_config），推荐的哨兵专注集：

```json
{
  "fields": {
    "SentryMode":      { "interval_seconds": 1 },
    "CenterDisplay":   { "interval_seconds": 30 },
    "ChargePortLatch": { "interval_seconds": 30 }
  }
}
```

值只在变化时传输，SentryMode 设 1 秒间隔也几乎不产生费用，事件端到端通常 1–3 秒，
信号差时更久。

## 诚实的期望管理

- 信号弱的停车场，推送可能延迟数分钟；
- 哨兵模式本身让车保持唤醒（~150W，每天掉电 4-8%），遥测流不额外增加耗电；
- OhmyTesla 解析官方 MQTT dispatcher 的 `{前缀}/{VIN}/v/SentryMode`，
  任何状态 → Aware 触发普通告警、→ Panic 触发严重告警；连续活动按会话合并
  （默认 15 秒窗口，持续 90 秒升级），并自动接管零配置的轮询层。
- **不接 Fleet Telemetry 也有普通哨兵告警**：哨兵开启时 OhmyTesla 每 5 秒只读查一次车
  （`center_display_state=7` 表示哨兵已触发、屏幕显示录像警告），不唤醒车、零配置，
  比 TeslaMate 自己停车时 60 秒一次快得多；但几秒内结束的短事件仍可能漏。
  接入实时流后这一层自动让位。


## ⚠️ 实施前必读（调研中确认的坑）

1. **Cloudflare Tunnel / Tailscale Funnel / ngrok 都用不了**。车辆用 mTLS 客户端证书直连，
   TLS 必须在 fleet-telemetry 进程上终结，而这些工具都在自己边缘终结 TLS。
   用 Cloudflare 做 DNS 可以，但该域名必须设为 **DNS only（灰云）**，不能开代理。
   → 需要**真正可入站的公网 IP**。国内家宽多为 CGNAT 且封 80/443，动手前先确认能不能入站。
2. **账单额度为 0 会导致 Tesla 删除你的遥测配置**，且官方明确说不会恢复——
   这会让告警**彻底静默**，而你自己的系统里看不到任何错误。务必设一个小的非零额度。
3. **`fleet_telemetry_config` 是全量替换**，不是增量合并。先 GET 现有配置，合并后再 POST 回去。
4. 证书用 **Let's Encrypt**；自签名未见成功案例。端口保持 **4443**，路由器上做**裸 TCP 转发**，
   前面不要放反向代理。
5. compose 里给 mosquitto 加健康检查并用 `depends_on: condition: service_healthy`——
   fleet-telemetry 的 MQTT 连接有 5 秒硬超时，broker 没起来它会直接 panic 崩溃重启。
6. **重连重放**：车辆断线重连后会重放最多数千条缓冲消息，而 MQTT 派发器丢掉了
   `created_at`/`is_resend` 字段，无法区分新旧。OhmyTesla 已实现 connectivity 主题门控
   （`FLEET_REPLAY_GATE_SECONDS`，默认 60 秒），否则一次断网就会连发一串假告警。
7. **中国大陆**：需在 developer.tesla.cn 用 +86 手机号单独注册，API 基地址不同。
   自托管 Fleet Telemetry 在国区是否可用，公开资料中**没有确定结论**——投入前请先书面确认。

## 主题与载荷格式（OhmyTesla 已按此实现）

- 字段主题：`{topic_base}/{VIN}/v/{FieldName}`，载荷是**裸 JSON 字符串**（含引号），
  例如 SentryMode 的值为 `"SentryModeStateAware"`；
- 连接状态：`{topic_base}/{VIN}/connectivity`，载荷含 `Status`（CONNECTED / DISCONNECTED / UNKNOWN）；
- 状态枚举：`SentryModeStateOff / Idle / Armed / Aware / Panic / Quiet`
  （OhmyTesla 对无前缀的旧固件写法也做了兼容）。

---

# 路线 A+：托管中转桥接（推荐，无需公网 IP）

这是 SentryPro / SentryAlert 那类 App 对用户零门槛的真正原因：**厂商自己托管了
fleet-telemetry 服务器**，车主只是授权 + 配对虚拟钥匙，车辆直接把遥测推给厂商。

OhmyTesla 用同样的思路，但数据落到你自己家里：

```
车辆 ──(Tesla 网络)──> 中转商的遥测服务器
                              │
        家里主动向外拉一条长连接（出站，无需任何入站端口）
                              ▼
              OhmyTesla 桥接 → 告警引擎 → 你的手机
```

## 配置（约 5 分钟）

```bash
# .env
FLEET_BRIDGE_ENABLED=true
FLEET_BRIDGE_PROVIDER=teslemetry
FLEET_BRIDGE_TOKEN=你的token
```

然后 `docker compose up -d`，在 **设置页**或 `GET /api/fleet/bridge/status` 查看连接状态。

### 各中转商对比

| 中转商 | 协议 | 本项目验证程度 | 价格 | 备注 |
|---|---|---|---|---|
| **Teslemetry** | SSE | ✅ 协议依其 OpenAPI 规范实现 | 约 $30/年/车 | 推荐；需在其后台把 `SentryMode` 字段打开（`interval_seconds=1`） |
| Tessie | WebSocket | ⚠️ 实验性，未用真实 token 验证 | $6.99/月/车 | 顺带提供签名指令，省掉自建代理 |
| MyTeslaMate | WebSocket | ⚠️ 实验性，协议读自其开源中继代码 | 按量约 €0.03/月 | 最便宜；注意必须用 `data:subscribe_all` 模式，文档里那个模式没有 SentryMode |

WebSocket 两家需要在 `requirements.txt` 里加 `websockets>=12`。
所有协议字段都用宽容解析（同时兼容 `stringValue` / `sentryModeStateValue` / 裸字符串），
所以就算某家格式微调也大概率能正常工作。

## 实现细节

- **重放过滤**：连接时的快照帧（`isCache: true`）直接丢弃；车辆断线重连会重放最多数千条
  缓冲消息，桥接用 `createdAt` 时间戳单调过滤——比 MQTT 那条路的"整段静默 60 秒"更精确，
  重连后 3 秒发生的真事件也不会被误杀。
- **退避重连**：指数退避 + 抖动，稳定运行满 60 秒才重置计数（防止"连上就断"的热循环）；
  401/403 直接退到最长间隔并记录，不会拿坏 token 反复冲击。
- **可观测性**：`/api/fleet/bridge/status` 给出每辆车的连接状态、最后事件时间、
  已丢弃的重放帧数、重连次数、最后错误。**桥接挂掉的表现就是"没有告警"，必须能看见。**

## 重要事实

- **一辆车可以同时配置 3-5 个遥测接收端**（Tesla 文档里两处说法分别是 3 和 5，按 3 规划），
  所以接入本项目**不影响你继续用 SentryPro / Tessie**。
- 建议保持 `SENTRY_POLLED_ALERTS=true`。有实时流时轮询层会自动让位（15 分钟内不重复告警），
  但在中转商故障、额度用尽、网络中断时它还能兜底，而且能帮你量化"轮询层到底漏了多少"。
- **中国大陆**：所有中转商都**没有明确说明**是否支持国区车辆（既没说支持也没说不支持），
  它们的文档里从未出现 tesla.cn。投入前请直接发邮件问：
  「你们是否在 developer.tesla.cn 注册了应用？能否服务中国大陆交付的特斯拉账号？」
  在得到肯定答复前，国区车主请以零配置的轮询层为准。
