# 安全

> [!CAUTION]
> **只适合家庭内网。** 8800 和 Grafana 绑在 `0.0.0.0`，同一个 Wi-Fi 下的任何设备都能直接访问。
> 绝对不要把这些端口映射到公网。

| 暴露面 | 认证 | 能拿到什么 |
|---|---|---|
| `:8800` | Basic（`ohmytesla` + 你设的密码） | 解锁车门、开后备箱、关哨兵、全部位置历史与录像 |
| `:8800/mcp` | 同一个密码 | 整个数据库，只读 |
| `:8800/healthz` | **有意豁免** | 只返回存活状态，不含任何车辆数据 |
| `:3000` Grafana | `./omt up` 生成的密码 | 全部数据面板 |
| `:4000` TeslaMate | **无，且它自身不支持** | 原生管理界面；默认**只有本机**打得开（`TESLAMATE_BIND_ADDR`） |
| mosquitto | 未映射到宿主机 | 只有 Docker 网络内可达，局域网连不上 |

`OHMYTESLA_PASSWORD` 对全站生效（除 `/healthz`），但**只管 8800**：

```bash
./omt grafana-password              # 已经跑起来的装机用这个，改 .env 对它无效
GF_SECURITY_ADMIN_PASSWORD=...      # 仅全新安装生效，数据卷初始化时读一次
MQTT_USERNAME=...                   # MQTT 认证，现在能透过 compose 传进去了
MQTT_PASSWORD=...
```

TeslaMate 完全没有 HTTP 认证选项 —— 它的 `runtime.exs` 只认 `DATABASE_*`、
`MQTT_*`、`SECRET_KEY_BASE` —— 而且它的页面能一键退出特斯拉登录。登录、地理围栏、
车辆设置都已经在 OhmyTesla 里了，所以 `:4000` 默认只有本机能开；要从别的设备打开，
在 `.env` 里设 `TESLAMATE_BIND_ADDR=0.0.0.0`。Grafana 也想只让本机访问，就在
`docker-compose.yml` 里它那行端口前加 `127.0.0.1:`（`BIND_ADDR` 会把 8800 一起锁上），
代价是手机和平板就访问不到 Grafana 了。

要在外面访问，就整套放进 VPN（Tailscale / WireGuard）或带认证的反向代理后面。
里面有你的位置历史、行车录像、推送渠道和车辆控制权。

> [!NOTE]
> `run_sql` 仍然能读你全部的行程与位置记录 —— 这是它的用途，不是缺陷。
