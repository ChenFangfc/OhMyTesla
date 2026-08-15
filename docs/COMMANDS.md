# 车辆控制指南

OhmyTesla 的「车辆控制」页可以直接给车下发指令：唤醒、鸣笛、闪灯、锁车/解锁、
空调开关与温度、充电起停、充电上限、充电口、车窗通风、前后备箱、哨兵开关。

## 零配置原理

TeslaMate 把你的 Tesla Token 用 `sha256(ENCRYPTION_KEY)` 做 AES-256-GCM 加密后
存在数据库里。本项目的 `docker-compose.yml` 让 hub 和 TeslaMate 共用同一个
`TM_ENCRYPTION_KEY`，因此 hub 能**自动读出并复用**这个 token——
你只要在 TeslaMate 里登录过，车辆控制就能直接用，不需要申请开发者账号、不需要填任何东西。

设置页会显示当前 token 来源：
- `自动复用 TeslaMate 的 token` —— 正常状态；
- `无可用 token` —— TeslaMate 还没登录，或两边 `ENCRYPTION_KEY` 不一致。

也可以在设置页手动粘贴一个 access token 覆盖（优先级最高）。

## 接口地址

`TESLA_API_HOST` 应与 TeslaMate 用的保持一致：

| 场景 | 值 |
|---|---|
| 全球默认 | `https://owner-api.teslamotors.com` |
| 中国大陆 | `https://owner-api.vn.cloud.tesla.cn` |
| 第三方代理（MyTeslaMate 等） | 代理提供的地址 |

## 签名指令（2021 年后车型）

Tesla 要求较新的车型通过 Fleet API 下发的指令必须**签名**。这需要额外部署官方
[vehicle-command](https://github.com/teslamotors/vehicle-command) HTTP 代理，
然后把 `TESLA_COMMAND_PROXY` 指向它：

```bash
# 示例：代理跑在同一台机器的 4430 端口
TESLA_COMMAND_PROXY=https://127.0.0.1:4430
```

没配代理时，需要签名的指令会返回明确的错误提示（不会静默失败）。
老车型（或走 owner-api 的路径）无需代理即可使用。

## 安全设计

- **敏感操作二次确认**：解锁、开前/后备箱、关闭哨兵会弹确认框，后端也强制要求
  `confirm` 标记（前端绕过也没用）；
- **防手抖**：同一指令 3 秒冷却；
- **车辆休眠**：返回 408 时会提示先「唤醒车辆」；
- **强烈建议设置 `OHMYTESLA_PASSWORD`**——这个页面能解锁你的车，绝不要在无认证状态下暴露到公网。

## 常见错误

| 提示 | 原因 |
|---|---|
| 没有可用的 Tesla API Token | TeslaMate 未登录，或 `ENCRYPTION_KEY` 不一致 |
| Token 已失效（401） | TeslaMate 刷新后会自动恢复；也可手动更新 |
| 车辆离线/休眠（408） | 先点「唤醒车辆」，等十几秒再重试 |
| 该车需要签名指令 | 见上文「签名指令」，需部署 vehicle-command 代理 |
