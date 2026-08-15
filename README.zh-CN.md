# ⚡ OhmyTesla

[English](README.md) · **中文**

自托管的特斯拉助手：行车记录仪多机位合成、哨兵告警推送、能耗与充电费用、车辆控制，
一个网页全包。

数据采集基于 [TeslaMate](https://github.com/teslamate-org/teslamate)，原封不动、可随时升级。AGPL-3.0。

## 安装

```bash
git clone <本仓库> && cd OhmyTesla
cp .env.example .env

# 生成门户密码，填进 .env 的 OHMYTESLA_PASSWORD=
openssl rand -base64 32 | tr -d '/+=' | cut -c1-28

./omt up
```

打开 **http://localhost:8800** —— 用户名 `ohmytesla`，密码就是刚才生成的那串。

> `OHMYTESLA_PASSWORD` 留空 = **整个界面不需要登录**，而它能解锁车门、关哨兵、
> 看你去过的每一个地方。8800 端口是整个局域网可达的，不只是本机。

## 连上你的车

特斯拉不允许第三方用账号密码登录，需要拿一次 token：

```bash
./tools/tesla_auth      # 弹出特斯拉官方登录页，密码只提交给特斯拉
```

把两串 token 粘进引导页点 Connect。之后就不用管了，会自动续期。

## 手机告警

**ntfy** —— 推荐，秒级到达。手机装 App，起一个别人猜不到的主题名，填进 `.env`：

```
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=omt-<随机串>                 # echo "omt-$(openssl rand -hex 10)"
PUBLIC_URL=http://192.168.1.20:8800     # 手机能访问到这台机器的地址，通知里的链接才点得动
```

**邮件**也支持（`SMTP_HOST/PORT/USER/PASS/FROM/TO`），Gmail 必须用
[应用专用密码](https://myaccount.google.com/apppasswords)而不是登录密码。
邮件延迟几分钟，当存档可以，**别拿它当哨兵告警的唯一渠道**——有人在动你车的时候，
迟到的邮件没有意义。

配完 `./omt restart`，然后 **Security 页 → Send test alert**。收不到的话那一页会告诉你缺哪一项。

> 主题名就是密码。ntfy.sh 是公开服务器，知道这串字符的人都能收到你的告警。

## 行车记录仪

把车上的 U 盘插到这台电脑，打开 **Dashcam → Browse USB drive** ——
浏览器直接读，不上传不拷贝。也可以把 `TeslaCam/` 整个拷进 `data/teslacam/`。

> 列出了录像却没画面？特斯拉可以加密行车记录仪录像。在车机
> **控制 → 安全 → 行车记录仪加密** 里关掉，之后新录的才能读。

## 充电花了多少钱

**设置 → 电价方案**：填一次单价（支持峰谷分时、按地点区分），点「重新核算」，
所有历史充电立刻有钱了。没覆盖到的时段显示「–」，而不是假装是 0。

## 让 AI 直接查你的车（MCP）

直接问「上个月充电花了多少」「这两周能耗为什么变差」，模型自己去查数据、自己交叉验证：

```bash
claude mcp add --transport http ohmytesla http://localhost:8800/mcp \
  --header "Authorization: Basic $(printf 'ohmytesla:%s' \
    "$(grep '^OHMYTESLA_PASSWORD=' .env | cut -d= -f2-)" | base64)"
```

15 个只读工具（行程 / 充电曲线 / 电池健康 / 停放耗电 / 胎压 / 告警 / 哨兵会话），
外加一个带护栏的 `run_sql`。四层护栏各自独立生效：

1. 只允许 `SELECT` / `WITH` 开头，不含写入类关键字，不含注释；
2. **降权到一个专用的非超级用户角色**（`omt_mcp_readonly`）执行 —— compose 给 hub 的是
   TeslaMate 自己的账号，那是个 superuser，不降权的话 `pg_read_file()` 能读容器里的
   任意文件，`private.tokens` 也读得到。降权后这两条实测都是 `permission denied`；
3. 只读事务，跑完一律回滚；
4. 服务端游标强制最多 200 行、8 秒超时。

数据全程不出你这台机器 —— 商业 App 做不到这件事，你的数据在他们服务器上。

## 常用命令

| | |
|---|---|
| `./omt up` · `down` | 启动 · 停止（数据保留） |
| `./omt status` | 各服务状态 + 数据还在不在流 |
| `./omt logs` | 跟日志（`./omt logs teslamate` 看采集端） |
| `./omt restart` | **改完 `.env`** 后重启 |
| `./omt update` | **改完代码**后重新构建 |
| `./omt backup` | 数据库导出到 `backups/` |
| `./omt grafana-password` | 改 Grafana 管理员密码 |

| 地址 | 登录 | |
|---|---|---|
| http://localhost:8800 | `ohmytesla` / `OHMYTESLA_PASSWORD` | OhmyTesla，日常只用这个 |
| http://localhost:3001 | `admin` / 见 `./omt grafana-password` | Grafana 深度面板 |
| http://localhost:4000 | 无 | TeslaMate 原生界面 |

按 **⌘K** 打开命令面板。

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
     （门户密码，用户名固定 ohmytesla；留空等于整个界面不设防）
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
- OHMYTESLA_PASSWORD 只保护 :8800。Grafana(3001) 和 TeslaMate(4000) 是独立容器，
  各有各的认证，设了一个不等于都护住了
````

## 安全

**只适合家庭内网。** 三个端口都绑在 `0.0.0.0`，同一个 Wi-Fi 下的任何设备都能直接访问。

| 暴露面 | 认证 | 能拿到什么 |
|---|---|---|
| `:8800` | Basic（`ohmytesla` + 你设的密码） | 解锁车门、开后备箱、关哨兵、全部位置历史与录像 |
| `:8800/mcp` | 同一个密码 | 整个数据库，只读 |
| `:8800/healthz` | **有意豁免** | 只返回存活状态，不含任何车辆数据 |
| `:3001` Grafana | 默认 `admin/admin` | 全部数据面板 |
| `:4000` TeslaMate | **无，且它自身不支持** | 原生管理界面，能改采集设置 |
| mosquitto | 未映射到宿主机 | 只有 Docker 网络内可达，局域网连不上 |

`OHMYTESLA_PASSWORD` 对全站生效（除 `/healthz`），但**只管 8800**：

```bash
./omt grafana-password              # 已经跑起来的装机用这个，改 .env 对它无效
GF_SECURITY_ADMIN_PASSWORD=...      # 仅全新安装生效，数据卷初始化时读一次
MQTT_USERNAME=...                   # MQTT 认证，现在能透过 compose 传进去了
MQTT_PASSWORD=...
```

TeslaMate 4.0.1 完全没有 HTTP 认证选项 —— 它的 `runtime.exs` 只认 `DATABASE_*`、
`MQTT_*`、`SECRET_KEY_BASE` —— 所以 `:4000` 只能靠「别让别人够到」来保护。
想让 3001/4000 只有本机能开，把 `docker-compose.yml` 里那两行端口前面加上
`127.0.0.1:`，代价是手机和平板就访问不到 Grafana 了。

**不要把这些端口直接映射到公网。** 要在外面访问就整套放进 VPN（Tailscale / WireGuard）
或带认证的反向代理后面。里面有你的位置历史、行车录像、推送渠道和车辆控制权。

> `run_sql` 仍然能读你全部的行程与位置记录 —— 这是它的用途，不是缺陷。

## 更多

- [使用流程](docs/QUICKSTART.md) · [架构设计](docs/ARCHITECTURE.md) ·
  [车辆控制](docs/COMMANDS.md) · [Fleet Telemetry](docs/FLEET-TELEMETRY.md)
- [功能矩阵](docs/FEATURE-MATRIX.md) —— 71 项功能 × 全竞品对照

## 许可与声明

AGPL-3.0（与 TeslaMate 一致）。

> This project is an unofficial community tool and is not affiliated with, endorsed by, or supported by the official TeslaMate project.

与 TeslaMate 团队及 Tesla, Inc. 均无隶属关系，未获任何一方背书。TeslaMate 与 Tesla
分别是其权利人的商标。本项目按 AGPL-3.0 以 Docker 镜像方式编排**未修改的** TeslaMate。
行车记录仪 SEI 解码为独立实现，字段定义参考特斯拉公开发布的 schema。
