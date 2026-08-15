# ⚡ OhmyTesla

自托管的特斯拉助手。行车记录仪多机位合成、哨兵告警推送、能耗与充电费用、车辆控制，一个网页全包。

基于 [TeslaMate](https://github.com/teslamate-org/teslamate) 做数据采集（原封不动、可随时升级），AGPL-3.0。

---

## 1. 装好并跑起来

```bash
git clone <本仓库> && cd OhmyTesla
cp .env.example .env
./omt up
```

打开 **http://localhost:8800**。

## 2. 连上你的车

第一次会自动跳到引导页。特斯拉不允许第三方用账号密码登录，需要拿一次 token：

```bash
./tools/tesla_auth      # 会弹出特斯拉官方登录页，密码只提交给特斯拉
```

把它给出的两串 token 粘进引导页，点 Connect。之后就不用管了，token 会自动续期。

## 3. 让告警推到手机上

### ntfy（推荐，秒级到达）

1. 手机装 **ntfy**（App Store / Play Store 搜，免费，不用注册）
2. 起一个别人猜不到的主题名，填进 `.env`：

   ```
   NTFY_URL=https://ntfy.sh
   NTFY_TOPIC=omt-换成一串随机字符
   ```

   > 主题名就是密码。ntfy.sh 是公开服务器，知道这串字符的人都能收到你的告警。
   > 生成一个：`echo "omt-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"`

3. `./omt restart`
4. App 里点 **+**，填同一个主题名，服务器保持默认

### 邮件（不用装 App，但会延迟几分钟）

Gmail 必须用「应用专用密码」，不是登录密码：

1. 开两步验证 → https://myaccount.google.com/signinoptions/two-step-verification
2. 生成应用专用密码 → https://myaccount.google.com/apppasswords
3. 填进 `.env`（那 16 位去掉空格）：

   ```
   SMTP_HOST=smtp.gmail.com
   SMTP_PORT=587
   SMTP_USER=你的邮箱@gmail.com
   SMTP_PASS=应用专用密码
   SMTP_FROM=你的邮箱@gmail.com
   SMTP_TO=你的邮箱@gmail.com
   ```

4. `./omt restart`

> **哨兵告警别只配邮件。** 邮件到手机的延迟取决于你邮件 App 多久拉一次，可能几分钟 ——
> 有人在动你车的时候，迟到的邮件没有意义。实时提醒交给 ntfy，邮件当存档。

### 让通知里的链接能点开

```
PUBLIC_URL=http://192.168.1.20:8800     # 换成手机能访问到这台机器的地址
```

不填的话通知里只有相对路径，手机上点不动。

### 验证

**Security 页 → Send test alert**，消息会立刻发到你配的所有渠道。收不到就回来看那一页的提示，它会告诉你缺哪一项。

## 4. 看行车记录仪

把 U 盘从车上拔下来插到这台电脑上，打开 **Dashcam → Browse USB drive**。
浏览器直接读，不上传不拷贝，看完再决定导入哪些。

也可以把 U 盘里的 `TeslaCam` 文件夹整个拷进 `data/teslacam/`。

> 如果显示「有几条录像但没画面」，页面会告诉你是不是解码不了 ——
> 特斯拉可以加密行车记录仪录像，在车机 **控制 → 安全 → 行车记录仪加密** 里关掉，之后新录的才能读。

## 5. 充电花了多少钱

**设置 → 电价方案**，填一次单价（支持峰谷分时、按地点区分），点「重新核算」，
所有历史充电立刻有钱了。不填的话费用一栏显示「–」而不是假装是 0。

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `./omt up` | 启动 |
| `./omt down` | 停止（数据保留） |
| `./omt status` | 各服务状态 + 数据还在不在流 |
| `./omt logs` | 跟日志（`./omt logs teslamate` 看采集端） |
| `./omt restart` | **改完 `.env`** 后重启 |
| `./omt update` | **改完代码**后重新构建 |
| `./omt backup` | 数据库导出到 `backups/` |

| 地址 | 用途 |
|---|---|
| http://localhost:8800 | OhmyTesla（日常只用这个） |
| http://localhost:3001 | Grafana 深度面板（进阶） |
| http://localhost:4000 | TeslaMate 原生界面（进阶） |

按 **⌘K** 打开命令面板：发指令、查数据、跳页面。

---

## 给 AI 助手的一段（复制粘贴即可）

如果你用 Claude Code / Cursor 之类的工具，把下面整段发给它，它会替你装好：

````text
帮我在这台机器上部署 OhmyTesla（自托管特斯拉助手）并配好手机推送。

前提：需要 Docker Desktop 正在运行。没有就先让我装。

一、启动
1. 在仓库根目录执行 `cp .env.example .env`
2. 生成并写入加密密钥与数据库密码（不要用示例值）：
   - TM_ENCRYPTION_KEY = `openssl rand -base64 32`
   - TM_DB_PASS        = `openssl rand -base64 18 | tr -d '/+='`
3. 端口若被占用，改 .env 里的 OHMYTESLA_PORT / GRAFANA_PORT / TESLAMATE_PORT
4. 执行 `./omt up`，它会等到应用真正可访问才返回

二、配 ntfy 推送（秒级到手机）
5. 生成一个随机主题名：`echo "omt-$(LC_ALL=C tr -dc 'a-z0-9' </dev/urandom | head -c 20)"`
6. 写进 .env：NTFY_URL=https://ntfy.sh 、NTFY_TOPIC=<上一步生成的>
7. 把本机局域网 IP 写进 PUBLIC_URL，形如 http://<IP>:8800
   （macOS: `ipconfig getifaddr en0`；Linux: `hostname -I | awk '{print $1}'`）
8. 执行 `./omt restart`（这个命令只重载 .env，不重新构建镜像）
9. 验证：`curl -s -X POST http://localhost:8800/api/alerts/test`
   期望返回 {"ok":true,"channels":["ntfy"]}
   再回读服务器确认真的送达（只看接口返回不够）：
   `curl -s "https://ntfy.sh/<主题名>/json?poll=1&since=5m"`
10. 把主题名告诉我，并提示我：手机装 ntfy App → 点 + → 填这个主题名

三、可选：邮件存档
11. 问我要 SMTP 信息。Gmail 必须用应用专用密码而不是登录密码，
    生成地址 https://myaccount.google.com/apppasswords （需先开两步验证）。
    拿到后写入 .env 的 SMTP_HOST/PORT/USER/PASS/FROM/TO，再 `./omt restart`。
    只填 SMTP_HOST 不填密码的话应用会明确报「配了一半」，不会假装在工作。

四、收尾
12. 告诉我打开 http://localhost:8800，按引导页粘贴 Tesla token 连接账号
    （token 用仓库里的 ./tools/tesla_auth 获取，特斯拉不接受账号密码登录）
13. 用 `./omt status` 确认五个容器都在跑

注意：
- 改了 .env 用 `./omt restart`；改了代码用 `./omt update`（带重新构建）
- ntfy 主题名等同于密码，别写进任何会公开的地方；.env 已在 .gitignore 里
````

---

## 安全

默认配置**只适合家庭内网**。要从外网访问，请放在 VPN（Tailscale / WireGuard）或带认证的反向代理后面 —— 这里面有你的位置历史、行车录像和推送渠道。

也可以在 `.env` 里设 `OHMYTESLA_PASSWORD` 开启全站基础认证。

## 更多

- [使用流程](docs/QUICKSTART.md) — 完整上手步骤、日常用法、常见问题
- [架构设计](docs/ARCHITECTURE.md) · [车辆控制](docs/COMMANDS.md) · [Fleet Telemetry](docs/FLEET-TELEMETRY.md)
- [功能矩阵](docs/FEATURE-MATRIX.md) — 71 项功能 × 全竞品对照

## 许可与声明

AGPL-3.0（与 TeslaMate 一致）。

与 TeslaMate 团队及 Tesla, Inc. 均无隶属关系，未获任何一方背书。TeslaMate 与 Tesla 分别是其权利人的商标。本项目按 AGPL-3.0 以 Docker 镜像方式编排**未修改的** TeslaMate。

行车记录仪 SEI 解码为独立实现，字段定义参考特斯拉公开发布的 schema。
