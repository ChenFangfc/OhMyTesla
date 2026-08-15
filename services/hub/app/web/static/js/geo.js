/* 坐标基准转换：WGS-84 ↔ GCJ-02（中国"火星坐标"）。
 *
 * 为什么需要：数据库里存的是 WGS-84（TeslaMate 原样落库），
 * 而高德/腾讯等国内地图用 GCJ-02。直接把 WGS 坐标画在国内地图上，
 * 轨迹会整体偏移 50-500 米——看起来像车开在河里或楼里。
 *
 * 关键原则：**只在渲染那一刻转换，绝不在入库时转换**。
 * 入库时转会永久污染地理围栏、破坏与上游 TeslaMate 的互通，
 * 而且用户换地图源之后会被转两次——这是一扇没有回头路的门。
 */
(function () {
  const A = 6378245.0, EE = 0.00669342162296594323;

  function outOfChina(lat, lon) {
    return lon < 72.004 || lon > 137.8347 || lat < 0.8293 || lat > 55.8271;
  }
  function tLat(x, y) {
    let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
    r += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
    r += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
    return r;
  }
  function tLon(x, y) {
    let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
    r += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
    r += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
    return r;
  }

  /* WGS-84 → GCJ-02。中国境外原样返回。 */
  function wgs2gcj(lat, lon) {
    if (outOfChina(lat, lon)) return [lat, lon];
    let dLat = tLat(lon - 105, lat - 35);
    let dLon = tLon(lon - 105, lat - 35);
    const rad = lat / 180 * Math.PI;
    let magic = Math.sin(rad);
    magic = 1 - EE * magic * magic;
    const sq = Math.sqrt(magic);
    dLat = (dLat * 180) / ((A * (1 - EE)) / (magic * sq) * Math.PI);
    dLon = (dLon * 180) / (A / sq * Math.cos(rad) * Math.PI);
    return [lat + dLat, lon + dLon];
  }

  /* 按当前地图源的基准转换一批点。datum: "wgs84" | "gcj02" */
  function project(points, datum) {
    if (datum !== "gcj02") return points;
    return points.map(([lat, lon]) => wgs2gcj(lat, lon));
  }

  window.omtGeo = { wgs2gcj, project, outOfChina };
})();
