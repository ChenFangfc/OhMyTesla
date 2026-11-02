# 让 AI 助手替你装好

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
    按引导页粘贴 Tesla token。token 用 tesla_auth（github.com/adriankumpf/tesla_auth，
    电脑）或 iPhone 上的 Auth app for Tesla 获取 —— 特斯拉不接受第三方用账号密码登录。
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
