# 社区 Grafana 面板

把任意 Grafana dashboard 的 JSON 丢进这个目录，`./omt restart` 之后会出现在
Grafana 的 **Community** 文件夹里。官方那 22 张面板不受影响，升级也不会冲突。

## 为什么仓库里没有预置面板

社区面板各有各的 license，而且跟着上游更新。把它们的 JSON 复制进来，等于
接管了别人的维护责任，还会在对方改了 SQL 之后悄悄给你看过期的数字。
这里只提供挂载点。

## 几个值得装的（自己下载）

| 面板包 | License | 装什么 |
|---|---|---|
| [jheredianet/Teslamate-CustomGrafanaDashboards](https://github.com/jheredianet/Teslamate-CustomGrafanaDashboards) | MIT | 14 张，含摊销成本、速度/温度能耗、直流充电曲线 |
| [CarlosCuezva/dashboards-Grafana-Teslamate](https://github.com/CarlosCuezva/dashboards-Grafana-Teslamate) | MIT | 成就徽章、生涯统计 |
| [wjsall/teslamate-chinese-dashboards](https://github.com/wjsall/teslamate-chinese-dashboards) | MIT | 45 张中文面板 + 分时电价 + GCJ-02 纠偏 |

```bash
# 例：装 jheredianet 那一包
cd grafana/community
curl -sLO https://github.com/jheredianet/Teslamate-CustomGrafanaDashboards/archive/refs/heads/main.tar.gz
tar xzf main.tar.gz --strip-components=2 --wildcards '*/dashboards/*.json'
rm main.tar.gz
cd ../.. && ./omt restart
```

## 注意

- 面板里的数据源名字必须是 `TeslaMate`（官方镜像里数据源就叫这个）。
  下载来的包如果写的是别的名字，Grafana 会显示 "Datasource not found"，
  在 JSON 里全局替换一下即可。
- 这些面板走的是同一个 Postgres 账号，**能看到你全部的位置历史**。
  装之前先扫一眼 SQL，别装来路不明的。
