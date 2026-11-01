<div align="center">

<img src="services/hub/app/web/static/icon.svg" width="76" height="76" alt="OhmyTesla">

# OhmyTesla

**自托管的特斯拉助手 —— 哨兵一触发就推到手机、3D 车辆控制、<br>行车记录仪多机位合成、能耗洞见、充电费用，一个网页全包。**

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue?style=flat-square)](LICENSE)
[![Built on TeslaMate](https://img.shields.io/badge/built_on-TeslaMate-cc0000?style=flat-square)](https://github.com/teslamate-org/teslamate)
[![Self-hosted](https://img.shields.io/badge/你的数据-不出这台机器-2ea44f?style=flat-square)](#安全)
[![MCP](https://img.shields.io/badge/MCP-15_个只读工具-8a63d2?style=flat-square)](#让-ai-直接查你的车mcp)

[English](README.md) · **中文**

</div>

---

数据采集基于 [TeslaMate](https://github.com/teslamate-org/teslamate) —— 原封不动，
随时可以自己升级。其余部分都是本项目。

**[安装](#安装)** · [连上你的车](#连上你的车) · [手机告警](#手机告警) ·
[车辆控制](#车辆控制) · [3D 车模](#3d-车模) ·
[行车记录仪](#行车记录仪) · [充电花了多少钱](#充电花了多少钱) ·
[AI 直查](#让-ai-直接查你的车mcp) · [常用命令](#常用命令) ·
[让 AI 替你装](#让-ai-助手替你装好) · [安全](#安全)

---

## 安装

```bash
git clone <本仓库> && cd OhmyTesla
cp .env.example .env

# 生成门户密码，填进 .env 的 OHMYTESLA_PASSWORD=
openssl rand -base64 32 | tr -d '/+=' | cut -c1-28

./omt up
```

打开 **http://localhost:8800** —— 用户名 `ohmytesla`，密码就是刚才生成的那串。

**Mac 上不想装 Docker**：原生 App 用一个 DMG 跑起整套（TeslaMate、Postgres、Mosquitto 和 hub），
见 [packaging/macos/README.md](packaging/macos/README.md)。

> [!WARNING]
> 这个界面能解锁车门、关哨兵、看你去过的每一个地方，所以 `OHMYTESLA_PASSWORD`
> 留空时它**干脆拒绝服务**（HTTP 503）。8800 端口是整个局域网可达的，不只是本机。

## 连上你的车

特斯拉不允许第三方用账号密码登录，需要拿一次 token：

```bash
./tools/tesla_auth      # 弹出特斯拉官方登录页，密码只提交给特斯拉
```

把两串 token 粘进引导页点 **Connect**。之后就不用管了，会自动续期。

## 手机告警

只有**哨兵触发**和**真正要紧的事**才推到手机，一行就能看懂发生了什么：

```text
🚨 Cybertruck — 锁车时车门被打开
家 · 21:41
```

| 推到手机 | 只在网页里记一笔 |
|---|---|
| 哨兵：有人在车旁、警报响起、锁着的车被开门或拉门把手 | 软件更新、电量低、保养到期 |
| 停着的车位置变了（被拖走/被开走） | 数据恢复、采集服务健康检查 |
| 哨兵告警暂停（收不到车的数据了） | 哨兵录像同步完成 |
| 胎压低、充电中途断电 | 其他日常诊断 |

每一类都能在 **安全 → 告警类型** 里改成「推送 / 仅网页 / 关闭」。
你自己回来开车（钥匙卡、手机钥匙解锁）不会被当成入侵；在家时默认只推升级了的事件，
可以在 **设置 → 哨兵告警** 里按地点改。

**渠道**在 **设置 → 通知** 里配，不用改 `.env`：ntfy（推荐，秒到）、Bark（iOS「关键警报」，
能穿透勿扰和静音）、Telegram、邮件、Webhook、Apprise。没有界面的部署照样可以用 `.env`
里的 `NTFY_*`、`SMTP_*` 这些变量，界面里填的优先。配完点 **安全 → 发送测试告警**。

`PUBLIC_URL` 填手机能访问到这台机器的地址，通知里的「打开」链接才点得动。

| 多快知道 | 延迟 | 需要什么 |
|---|---|---|
| TeslaMate 轮询 | 约 1 分钟 | 什么都不用 |
| 哨兵开着时快速检查 | 约 10 秒 | 什么都不用（只读 TeslaMate 的 token，不唤醒车） |
| Fleet Telemetry | 1～2 秒 | 公网服务器或中转服务，见 [docs/FLEET-TELEMETRY.md](docs/FLEET-TELEMETRY.md) |

> [!IMPORTANT]
> ntfy 的主题名就是密码。ntfy.sh 是公开服务器，知道这串字符的人都能收到你的告警。
> 设置页里「生成新主题」给的是随机串。

## 车辆控制

**概览** 页的 3D 车模上直接点车门、前备箱、充电口；**控制** 页把空调、座椅、充电、车窗、
哨兵放在一页。Cybertruck 另有货箱卷帘、车外插座、对外供电、灯条、悬架高度。

2021 年以后的车（含全部 Cybertruck）只认签名指令，需要一次性在 **设置 → 车辆指令**
里走完 6 步（特斯拉开发者应用 + 公钥 + 登录授权 + 手机上批准虚拟钥匙），
不用另外部署代理。步骤和常见问题见 [docs/COMMANDS.md](docs/COMMANDS.md)。

## 3D 车模

自带 Cybertruck 车模（[DriveOS](https://branzoom.github.io/driveos)，CC BY 4.0）。
其他车型在 **设置 → 车辆 → 3D 车模** 上传一个 `.glb` 就会替换上去；
要能开门开盖，模型的部件得按约定命名，见
[static/models/README.md](services/hub/app/web/static/models/README.md)，
[`scripts/models/rig_vehicle.py`](scripts/models/rig_vehicle.py) 能把 Blender 里的模型转成这个格式。

## 行车记录仪

把车上的 U 盘插到这台电脑，打开 **Dashcam → Browse USB drive** ——
浏览器直接读，不上传不拷贝。也可以把 `TeslaCam/` 整个拷进 `data/teslacam/`。

> [!NOTE]
> 列出了录像却没画面？特斯拉可以加密行车记录仪录像。在车机
> **控制 → 安全 → 行车记录仪加密** 里关掉，之后新录的才能读。

## 充电花了多少钱

**设置 → 电价方案** —— 填一次单价（支持峰谷分时、按地点区分），点「重新核算」，
所有历史充电立刻有钱了。没覆盖到的时段显示 `–`，而不是假装是 0。

## 让 AI 直接查你的车（MCP）

直接问*「上个月充电花了多少」*「这两周能耗为什么变差」，模型自己去查数据、自己交叉验证：

```bash
claude mcp add --transport http ohmytesla http://localhost:8800/mcp \
  --header "Authorization: Basic $(printf 'ohmytesla:%s' \
    "$(grep '^OHMYTESLA_PASSWORD=' .env | cut -d= -f2-)" | base64)"
```

15 个只读工具 —— 行程、充电曲线、电池健康、停放耗电、胎压、告警、哨兵会话 ——
外加一个带护栏的 `run_sql`，四层护栏各自独立生效：

| # | 护栏 |
|---|---|
| 1 | 只允许 `SELECT` / `WITH` 开头；不含写入类关键字，不含注释 |
| 2 | 降权到一个专用的**非超级用户角色**（`omt_mcp_readonly`）执行 |
| 3 | 只读事务，跑完一律回滚 |
| 4 | 服务端游标强制最多 200 行、8 秒超时 |

第 2 层最关键：compose 给 hub 的是 TeslaMate 自己的账号，那**是个 superuser**。
不降权的话 `pg_read_file()` 能读容器里的任意文件，`private.tokens` 也读得到。
降权后这两条实测都是 `permission denied`。

数据全程不出你这台机器 —— 商业 App 做不到这件事，你的数据在他们服务器上。

## 常用命令

| 命令 | 作用 |
|---|---|
| `./omt up` · `down` | 启动 · 停止（数据保留） |
| `./omt status` | 各服务状态 + 数据还在不在流 |
| `./omt logs` | 跟日志（`./omt logs teslamate` 看采集端） |
| `./omt restart` | **改完 `.env`** 后重启 |
| `./omt update` | **改完代码**后重新构建 |
| `./omt backup` | 数据库导出到 `backups/` |
| `./omt grafana-password` | 改 Grafana 管理员密码 |

| 地址 | 登录 | 是什么 |
|---|---|---|
| <http://localhost:8800> | `ohmytesla` / `OHMYTESLA_PASSWORD` | OhmyTesla，日常只用这个 |
| <http://localhost:3000> | `admin` / 见 `./omt grafana-password` | Grafana 深度面板（端口见 `GRAFANA_PORT`） |
| <http://localhost:4000> | 无 | TeslaMate 原生界面 |

按 <kbd>⌘</kbd><kbd>K</kbd> 打开命令面板。

## 让 AI 助手替你装好

用 Claude Code / Cursor 的话，把下面整段发给它，它会替你装好：

````text
帮我在这台机器上部署 OhmyTesla（自托管特斯拉助手）并配好手机推送。

前提：需要 Docker Desktop 正在运行。没有就先让我装。

1. 在仓库根目录执行 `cp .env.example .env`
2. 生成并写入下面三项（不要用示例值，也不要留空）：
   - TM_ENCRYPTION_KEY   = `openssl rand -base64 32`
   - TM_DB_PASS          = `openssl rand -base64 18 | tr -d '/+='`
   - OHMYTESLA_PASSWORD  = `openssl rand -base64 32 | tr -d '/+=' | cut -c1-28`
     （门户密码，用户名固定 ohmytesla；留空的话门户直接拒绝服务）
3. 端口若被占用，改 .env 里的 OHMYTESLA_PORT / GRAFANA_PORT / TESLAMATE_PORT
4. 执行 `./omt up`，它会等到应用真正可访问才返回
5. 生成 ntfy 主题名：`echo "omt-$(openssl rand -hex 10)"`
   连同 NTFY_URL=https://ntfy.sh 一起写进 .env 的 NTFY_TOPIC
6. 把本机局域网 IP 写进 PUBLIC_URL，形如 http://<IP>:8800
   （macOS: `ipconfig getifaddr en0`；Linux: `hostname -I | awk '{print $1}'`）
7. 执行 `./omt restart`（只重载 .env，不重新构建镜像）
8. 验证。设了门户密码就必须带 -u，否则一律 401：
   `curl -s -u ohmytesla:<密码> -X POST http://localhost:8800/api/alerts/test`
   期望返回 {"ok":true,"channels":["ntfy"]}
   再回读服务器确认真的送达（只看接口返回不算数）：
   `curl -s "https://ntfy.sh/<主题名>/json?poll=1&since=5m"`
9. 把主题名和门户密码告诉我，并提示我：手机装 ntfy App → 点 + → 填这个主题名
10. 告诉我打开 http://localhost:8800（浏览器会弹认证框：用户名 ohmytesla + 上面的密码），
    按引导页粘贴 Tesla token。token 用 `./tools/tesla_auth` 获取 ——
    特斯拉不接受第三方用账号密码登录。
11. 用 `./omt status` 确认五个容器都在跑

可选的邮件存档：问我要 SMTP 信息。Gmail 必须用应用专用密码
（https://myaccount.google.com/apppasswords，需先开两步验证），拿到后写入
SMTP_HOST/PORT/USER/PASS/FROM/TO 再 `./omt restart`。只填 SMTP_HOST 不填密码的话，
应用会明确报「配了一半」，不会假装在工作。

注意：
- 改了 .env 用 `./omt restart`；改了代码用 `./omt update`（带重新构建）
- ntfy 主题名等同于密码，别写进任何会公开的地方；.env 已在 .gitignore 里
- OHMYTESLA_PASSWORD 只保护 :8800。Grafana(3000) 和 TeslaMate(4000) 是独立容器，
  各有各的认证，设了一个不等于都护住了
````

## 安全

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

## 文档

| | |
|---|---|
| [使用流程](docs/QUICKSTART.md) | 完整上手步骤、日常用法、常见问题 |
| [架构设计](docs/ARCHITECTURE.md) | 各部分怎么拼起来的 |
| [车辆控制](docs/COMMANDS.md) | 一次性设置签名指令，安全设计，常见问题 |
| [teslausb](docs/TESLAUSB.md) | 车上的 U 盘自动把录像同步到这台机器 |
| [Fleet Telemetry](docs/FLEET-TELEMETRY.md) | 流式采集配置 |
| [功能矩阵](docs/FEATURE-MATRIX.md) | 71 项功能 × 全竞品对照 |

## 许可与声明

[AGPL-3.0](LICENSE)（与 TeslaMate 一致）。

> This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project.

与 TeslaMate 团队及 Tesla, Inc. 均无隶属关系，未获任何一方背书。TeslaMate 与 Tesla
分别是其权利人的商标。本项目按 AGPL-3.0 以 Docker 镜像方式编排**未修改的** TeslaMate。
行车记录仪 SEI 解码为独立实现，字段定义参考特斯拉公开发布的 schema。
