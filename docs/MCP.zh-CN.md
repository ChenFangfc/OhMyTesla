# 让 AI 直接查你的车（MCP）

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
