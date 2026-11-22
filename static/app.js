/* SysBoard 前端逻辑：每 2 秒拉取 /api/stats 并渲染各卡片；项目卡片支持启停与看日志 */
(function () {
  "use strict";

  var POLL_MS = 2000;
  var HISTORY = 60;
  var TWEEN_MS = 450;
  var LOG_MS = 3000;
  var LOG_TAIL = 300;

  var C = {
    accent: "#D97757",
    blue: "#5B84B1",
    green: "#6A994E",
    yellow: "#D9A03F",
    red: "#C44536",
    grid: "#E3E1D8",
    track: "#EFEDE4",
    text: "#141413",
    text2: "#6B6A66",
    text3: "#9C9A93",
    card: "#FAF9F5"
  };

  // 图表过渡：数据每 2 秒变化时做 500ms 的缓动形变
  var ANIM = {
    animation: true,
    animationDuration: 500,
    animationDurationUpdate: 500,
    animationEasing: "cubicOut",
    animationEasingUpdate: "cubicOut"
  };

  var hist = {
    t: [], cpu: [], mem: [], read: [], write: [], down: [], up: [], temp: []
  };
  var charts = {};
  var lastStats = null;
  var renderError = null;
  // 两张进程表各自的排序状态
  var sorts = {
    proc: { key: "cpu", dir: -1, body: "proc-body", source: "procs" },
    memproc: { key: "mem", dir: -1, body: "memproc-body", source: "mem_procs" }
  };

  /* ---------- 工具 ---------- */

  function $(id) { return document.getElementById(id); }

  // 写文本统一走这里：元素缺失时静默跳过，不让一个空引用毁掉整张卡片。
  // （ECharts 初始化会清空图表容器内的子元素，覆盖层元素因此可能整批消失）
  function setText(id, text) {
    var el = $(id);
    if (el) { el.textContent = text; }
  }

  function hexA(hex, alpha) {
    var r = parseInt(hex.slice(1, 3), 16);
    var g = parseInt(hex.slice(3, 5), 16);
    var b = parseInt(hex.slice(5, 7), 16);
    return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
  }

  function area(color) {
    return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
      { offset: 0, color: hexA(color, 0.28) },
      { offset: 1, color: hexA(color, 0.02) }
    ]);
  }

  // 大数字补间：值变化时从旧值平滑滚到新值，重复触发会取消上一段
  function tweenNumber(el, target, decimals) {
    // 元素缺失（页面残缺或旧版缓存）时静默跳过，别让一个空引用拖垮整页
    if (!el) { return; }
    if (el._raf) { cancelAnimationFrame(el._raf); }
    var from = parseFloat(el.textContent);
    if (isNaN(from)) { from = 0; }
    if (from === target) { el.textContent = target.toFixed(decimals); return; }
    var start = performance.now();
    function frame(now) {
      // 时钟异常时 (now - start) 可能为负或跳变，夹到 [0,1] 免得算出离谱数值
      var p = Math.min(1, Math.max(0, (now - start) / TWEEN_MS));
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = (from + (target - from) * eased).toFixed(decimals);
      if (p < 1) {
        el._raf = requestAnimationFrame(frame);
      } else {
        el._raf = null;
        el.textContent = target.toFixed(decimals);
      }
    }
    el._raf = requestAnimationFrame(frame);
  }

  function fmtRate(bps) {
    if (bps == null) { return "–"; }
    if (bps < 1024) { return bps.toFixed(0) + " B/s"; }
    if (bps < 1048576) { return (bps / 1024).toFixed(1) + " KB/s"; }
    return (bps / 1048576).toFixed(2) + " MB/s";
  }

  function fmtAxisRate(bps) {
    if (bps >= 1048576) { return (bps / 1048576).toFixed(1) + "M"; }
    if (bps >= 1024) { return (bps / 1024).toFixed(0) + "K"; }
    return bps.toFixed(0);
  }

  // 字节数自动切换单位：B → KB → MB → GB
  function fmtSize(bytes) {
    if (bytes == null || isNaN(bytes)) { return "–"; }
    if (bytes < 1024) { return bytes.toFixed(0) + " B"; }
    var units = ["KB", "MB", "GB", "TB"];
    var value = bytes / 1024;
    var i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return (value >= 100 ? value.toFixed(0) : value.toFixed(1)) + " " + units[i];
  }

  function fmtUptime(sec) {
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) { return d + " 天 " + h + " 小时"; }
    if (h > 0) { return h + " 小时 " + m + " 分"; }
    return m + " 分钟";
  }

  function timeLabel(ts) {
    var d = new Date(ts * 1000);
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function push(arr, value) {
    arr.push(value);
    if (arr.length > HISTORY) { arr.shift(); }
  }

  /* ---------- 图表模板 ---------- */

  var TOOLTIP = {
    trigger: "axis",
    backgroundColor: C.card,
    borderColor: C.grid,
    textStyle: { color: C.text, fontSize: 12 },
    axisPointer: { lineStyle: { color: C.text3 } }
  };

  function catAxis() {
    return {
      type: "category",
      boundaryGap: false,
      data: hist.t,
      axisLine: { lineStyle: { color: C.grid } },
      axisTick: { show: false },
      axisLabel: { color: C.text3, fontSize: 10 }
    };
  }

  function valAxis(max, formatter) {
    return {
      type: "value",
      min: 0,
      max: max,
      axisLabel: { color: C.text3, fontSize: 10, formatter: formatter },
      splitLine: { lineStyle: { color: C.grid, type: "dashed" } }
    };
  }

  var GRID = { left: 42, right: 12, top: 16, bottom: 22 };

  function lineSeries(name, color, data, withArea) {
    return {
      name: name,
      type: "line",
      smooth: true,
      showSymbol: false,
      data: data,
      lineStyle: { color: color, width: 2 },
      itemStyle: { color: color },
      areaStyle: withArea ? { color: area(color) } : null
    };
  }

  // 饼图数据必须逐项带 itemStyle.color：ECharts 更新时整体替换 data 数组，
  // 不带颜色的项会回落到默认调色板（蓝/绿），环形图就不是主题色了。
  function donutData(used, free, usedColor, usedName, freeName) {
    return [
      { value: used, name: usedName, itemStyle: { color: usedColor } },
      { value: free, name: freeName, itemStyle: { color: C.track } }
    ];
  }

  function initCharts() {
    charts.cores = echarts.init($("chart-cores"));
    charts.cpu = echarts.init($("chart-cpu"));
    charts.mem = echarts.init($("chart-mem"));
    charts.memHist = echarts.init($("chart-mem-history"));
    charts.disk = echarts.init($("chart-disk"));
    charts.diskIo = echarts.init($("chart-disk-io"));
    charts.net = echarts.init($("chart-net"));
    charts.temp = echarts.init($("chart-temp"));

    charts.cores.setOption(Object.assign({
      grid: { left: 8, right: 8, top: 10, bottom: 20 },
      xAxis: {
        type: "category",
        data: [],
        axisLabel: { color: C.text3, fontSize: 10 },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: C.grid } }
      },
      yAxis: { type: "value", min: 0, max: 100, axisLabel: { show: false }, splitLine: { show: false } },
      series: [{
        type: "bar",
        barWidth: "52%",
        showBackground: true,
        backgroundStyle: { color: C.track, borderRadius: 4 },
        itemStyle: { color: C.accent, borderRadius: 4 },
        data: []
      }]
    }, ANIM));

    charts.cpu.setOption(Object.assign({
      tooltip: TOOLTIP,
      grid: GRID,
      xAxis: catAxis(),
      yAxis: valAxis(100, "{value}%"),
      series: [lineSeries("CPU 使用率", C.accent, hist.cpu, true)]
    }, ANIM));

    charts.memHist.setOption(Object.assign({
      tooltip: TOOLTIP,
      grid: GRID,
      xAxis: catAxis(),
      yAxis: valAxis(100, "{value}%"),
      series: [lineSeries("内存使用率", C.accent, hist.mem, true)]
    }, ANIM));

    charts.diskIo.setOption(Object.assign({
      tooltip: TOOLTIP,
      grid: GRID,
      xAxis: catAxis(),
      yAxis: valAxis(null, fmtAxisRate),
      series: [
        lineSeries("读取", C.blue, hist.read, true),
        lineSeries("写入", C.accent, hist.write, true)
      ]
    }, ANIM));

    charts.net.setOption(Object.assign({
      tooltip: TOOLTIP,
      grid: GRID,
      xAxis: catAxis(),
      yAxis: valAxis(null, fmtAxisRate),
      series: [
        lineSeries("下载", C.blue, hist.down, true),
        lineSeries("上传", C.accent, hist.up, true)
      ]
    }, ANIM));

    charts.temp.setOption(Object.assign({
      tooltip: TOOLTIP,
      grid: GRID,
      xAxis: catAxis(),
      yAxis: {
        type: "value",
        scale: true,
        min: function (v) { return Math.floor(v.min - 5); },
        axisLabel: { color: C.text3, fontSize: 10, formatter: "{value}°" },
        splitLine: { lineStyle: { color: C.grid, type: "dashed" } }
      },
      series: [{
        name: "SoC 温度",
        type: "line",
        smooth: true,
        showSymbol: false,
        data: hist.temp,
        lineStyle: { color: C.accent, width: 2 },
        itemStyle: { color: C.accent },
        areaStyle: { color: area(C.accent) },
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: C.red, type: "dashed", width: 1 },
          label: { color: C.red, fontSize: 10, formatter: "80°C" },
          data: [{ yAxis: 80 }]
        }
      }]
    }, ANIM));

    charts.disk.setOption(Object.assign({
      animationDuration: 600,
      tooltip: { trigger: "item", backgroundColor: C.card, borderColor: C.grid,
        textStyle: { color: C.text, fontSize: 12 } },
      series: [{
        type: "pie",
        radius: ["58%", "82%"],
        center: ["50%", "50%"],
        avoidLabelOverlap: false,
        itemStyle: { borderColor: C.card, borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: false } },
        data: donutData(0, 100, C.accent, "已用", "空闲")
      }]
    }, ANIM));

    charts.mem.setOption(Object.assign({
      animationDuration: 600,
      tooltip: { trigger: "item", backgroundColor: C.card, borderColor: C.grid,
        textStyle: { color: C.text, fontSize: 12 } },
      series: [
        donutSeries("27%", "内存"),
        donutSeries("73%", "Swap")
      ]
    }, ANIM));
  }

  function donutSeries(center, name) {
    return {
      name: name,
      type: "pie",
      radius: ["56%", "78%"],
      center: [center, "50%"],
      avoidLabelOverlap: false,
      itemStyle: { borderColor: C.card, borderWidth: 2 },
      // 中心文字由 HTML 覆盖层（.donut-center）常驻显示，canvas 里不画标签
      label: { show: false },
      emphasis: { label: { show: false } },
      data: donutData(0, 100, name === "内存" ? C.accent : C.blue, "已用", "空闲")
    };
  }

  /* ---------- 渲染 ---------- */

  function renderHost(h) {
    setText("subtitle",
      h.hostname + " · " + (h.model || "Linux") + " · 已运行 " + fmtUptime(h.uptime_sec) +
      " · 负载 " + h.load.join(" / "));
  }

  function renderCpu(cpu) {
    tweenNumber($("cpu-percent"), +cpu.percent.toFixed(1), 1);
    setText("cpu-note",
      cpu.count + " 核" + (cpu.freq_mhz ? " · " + (cpu.freq_mhz / 1000).toFixed(2) + " GHz" : ""));

    charts.cores.setOption({
      xAxis: { data: cpu.per_cpu.map(function (_, i) { return "CPU" + i; }) },
      series: [{ data: cpu.per_cpu.map(function (v) { return +v.toFixed(1); }) }]
    });
    charts.cpu.setOption({
      xAxis: { data: hist.t },
      series: [{ data: hist.cpu }]
    });
  }

  function renderMem(mem, swap) {
    setText("mem-note", fmtSize(mem.used) + " / " + fmtSize(mem.total));
    tweenNumber($("mem-percent"), +mem.percent.toFixed(1), 1);
    tweenNumber($("swap-percent"), +swap.percent.toFixed(1), 1);
    charts.mem.setOption({
      series: [
        { data: donutData(+mem.percent.toFixed(1), +(100 - mem.percent).toFixed(1), C.accent, "已用", "空闲") },
        { data: donutData(+swap.percent.toFixed(1), +(100 - swap.percent).toFixed(1), C.blue, "已用", "空闲") }
      ]
    });
    charts.memHist.setOption({
      xAxis: { data: hist.t },
      series: [{ data: hist.mem }]
    });
  }

  function renderDisk(disk) {
    var root = disk.root;
    $("disk-read").textContent = fmtRate(disk.read_bps);
    $("disk-write").textContent = fmtRate(disk.write_bps);
    tweenNumber($("disk-percent"), +root.percent.toFixed(1), 1);
    setText("disk-detail", "已用 " + fmtSize(root.used));
    // 各挂载点逐行列出（根分区、启动分区、插上的硬盘等）
    var mountEl = $("mount-list");
    if (mountEl) {
      mountEl.innerHTML = (disk.mounts || []).map(function (m) {
        var name = m.label || m.dev.replace("/dev/", "");
        return "<div class='mount-row'><span class='mount-name' title='" + esc(m.dev) + "'>" + esc(name) +
          "</span><span class='mount-point'>" + esc(m.mount) + "</span>" +
          "<span class='mount-bar'><i style='width:" + Math.min(100, m.percent) + "%'></i></span>" +
          "<span class='mount-size'>" + fmtSize(m.used) + " / " + fmtSize(m.total) + "</span></div>";
      }).join("");
    }
    // 有未挂载的分区时，按钮上挂个数字角标提醒
    var pending = (disk.available || []).length;
    var badge = $("disk-pending");
    if (badge) {
      badge.textContent = pending ? String(pending) : "";
      badge.hidden = !pending;
    }
    charts.disk.setOption({
      series: [{
        data: donutData(
          +root.percent.toFixed(1),
          +(100 - root.percent).toFixed(1),
          C.accent,
          "已用 " + fmtSize(root.used),
          "空闲 " + fmtSize(root.total - root.used)
        )
      }]
    });
    renderDrives(disk);
    charts.diskIo.setOption({
      xAxis: { data: hist.t },
      series: [{ data: hist.read }, { data: hist.write }]
    });
  }

  /* ---------- 每块物理盘（读写 / 忙碌 / SMART 温度） ---------- */

  function shortModel(model) {
    // 卡片窄，过长的型号名可适当简写，全称放 title
    var text = String(model || "").trim().replace(/^WDC\s+/i, "");
    return text.split("-")[0] || text;
  }

  // 机械盘温度分档：48/55 °C 是普通 3.5 寸盘（本机的 WD5000AAKX 标称上限 60 °C）
  // 合适的经验阈值，和 SoC 的 70/80 不是一套，别混用
  function tempGrade(celsius) {
    if (celsius == null) { return "na"; }
    if (celsius >= 55) { return "hot"; }
    if (celsius >= 48) { return "warm"; }
    return "ok";
  }

  // 温度是 udisksd 的 SMART 缓存值，采样时间放 title 里，免得被当成实时读数
  function smartNote(drive) {
    var when = drive.temp_updated ? "SMART 采样于 " + timeLabel(drive.temp_updated) : "SMART 无采样时间";
    return when + (drive.temp_failing ? " · SMART 已报告异常" : "");
  }

  // udisksd 每 10 分钟才重采一次 SMART（实测 SmartUpdated 每 600 秒前进），
  // 所以温度最多差十分钟。把延迟写在数值后面，避免被当成秒级实时值
  function smartAge(drive) {
    if (!drive || !drive.temp_updated) { return ""; }
    var sec = Math.round(Date.now() / 1000 - drive.temp_updated);
    if (sec < 90) { return "（刚刚）"; }
    return "（" + Math.round(sec / 60) + " 分钟前）";
  }

  function firstTempDrive(drives) {
    var list = drives || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].temp_c != null) { return list[i]; }
    }
    return null;
  }

  function renderDrives(disk) {
    var el = $("drive-list");
    if (!el) { return; }
    var drives = disk.drives || [];
    if (!drives.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = drives.map(function (d) {
      // 没有 SMART 温度的盘（SD 卡、U 盘）不渲染空标签，避免看着像"读数失败"
      var tempHtml = d.temp_c == null ? "" :
        "<span class='drive-temp " + tempGrade(d.temp_c) + "' title='" + esc(smartNote(d)) + "'>" +
          Math.round(d.temp_c) + "°C</span>";
      var busy = d.busy == null ? "" : "<span class='drive-busy'>忙 " + Math.round(d.busy) + "%</span>";
      var detail = (d.model || d.dev) + (d.serial ? " · " + d.serial : "") +
        (d.size ? " · " + fmtSize(d.size) : "");
      return "<div class='drive-row'>" +
        "<div class='drive-head'>" +
          "<span class='drive-name'>" + esc(d.name) + "</span>" +
          "<span class='drive-model' title='" + esc(detail) + "'>" + esc(shortModel(d.model) || d.dev) + "</span>" +
          tempHtml +
        "</div>" +
        "<div class='drive-io'>" +
          "<span class='io-down'>读 " + fmtRate(d.read_bps) + "</span>" +
          "<span class='io-up'>写 " + fmtRate(d.write_bps) + "</span>" + busy +
        "</div>" +
        "<div class='drive-total'>自开机 读 " + fmtSize(d.read_bytes) + " · 写 " + fmtSize(d.write_bytes) + "</div>" +
        "</div>";
    }).join("");
  }

  function primaryNet(net) {
    var names = Object.keys(net).sort(function (a, b) {
      if (a === "eth0") { return -1; }
      if (b === "eth0") { return 1; }
      return 0;
    });
    return names.length ? { name: names[0], data: net[names[0]] } : null;
  }

  function renderNet(net, totals, total, host) {
    var primary = primaryNet(net);
    var name = primary ? primary.name : null;
    var cur = primary ? primary.data : { up_bps: 0, down_bps: 0 };
    setText("net-note", name || "无活动网卡");
    setText("net-down", fmtRate(cur.down_bps));
    setText("net-up", fmtRate(cur.up_bps));
    // 累计取内核计数器，重启归零；合计只含物理网卡（口径见 README）
    var sum = total || { down_bytes: 0, up_bytes: 0 };
    setText("net-down-total", fmtSize(sum.down_bytes));
    setText("net-up-total", fmtSize(sum.up_bytes));
    var foot = $("net-foot");
    if (foot) {
      var alive = host && host.uptime_sec ? " · 自开机 " + fmtUptime(host.uptime_sec) : "";
      foot.textContent = "合计 " + fmtSize(sum.down_bytes + sum.up_bytes) + alive;
      // 逐网卡明细挂 title：Docker 桥这类内部网卡只在这里列，不进合计
      foot.title = Object.keys(totals || {}).sort().map(function (nic) {
        var row = totals[nic];
        return nic + "  ↓ " + fmtSize(row.down_bytes) + "  ↑ " + fmtSize(row.up_bytes);
      }).join("\n") || "暂无网卡数据";
    }
    charts.net.setOption({
      xAxis: { data: hist.t },
      series: [{ data: hist.down }, { data: hist.up }]
    });
  }

  function renderTemp(temp, drives) {
    var sub = $("temp-drive");
    if (sub) {
      var hdd = firstTempDrive(drives);
      if (hdd) {
        sub.textContent = (hdd.rotational ? "机械盘 " : "盘 ") + hdd.name +
          " " + hdd.temp_c.toFixed(1) + " °C" + smartAge(hdd);
        sub.className = "temp-sub " + tempGrade(hdd.temp_c);
        sub.title = (hdd.model || hdd.dev) + " · " + smartNote(hdd);
      } else {
        sub.textContent = "机械盘温度 –";
        sub.className = "temp-sub";
        sub.title = "没拿到 SMART 温度：udisks2/busctl 不可用，或这块盘不支持 SMART";
      }
    }
    var el = $("temp-value");
    if (temp == null) {
      el.textContent = "–";
      return;
    }
    tweenNumber(el, +temp.toFixed(1), 1);
    el.style.color = temp >= 80 ? C.red : temp >= 70 ? C.yellow : C.text;
    var lineColor = temp >= 80 ? C.red : temp >= 70 ? C.yellow : C.accent;
    charts.temp.setOption({
      xAxis: { data: hist.t },
      series: [{
        data: hist.temp,
        lineStyle: { color: lineColor, width: 2 },
        itemStyle: { color: lineColor },
        areaStyle: { color: area(lineColor) }
      }]
    });
  }

  function renderProcTable(which, procs) {
    var state = sorts[which];
    var sorted = procs.slice().sort(function (a, b) {
      if (typeof a[state.key] === "string") {
        return state.dir * a[state.key].localeCompare(b[state.key]);
      }
      return state.dir * (a[state.key] - b[state.key]);
    });
    $(state.body).innerHTML = sorted.map(function (p) {
      var cells = "<td>" + p.pid + "</td>" +
        "<td class='left name' title='" + esc(p.name) + "'>" + esc(p.name) + "</td>" +
        "<td class='left'>" + esc(p.user) + "</td>";
      if (which === "memproc") {
        cells += "<td>" + p.mem.toFixed(1) + "</td><td class='size'>" + fmtSize(p.rss) + "</td>";
      } else {
        cells += "<td>" + p.cpu.toFixed(1) + "</td><td>" + p.mem.toFixed(1) + "</td>";
      }
      return "<tr>" + cells + "</tr>";
    }).join("");
  }

  /* ---------- 项目管理 ---------- */

  // 卡片 DOM 只在建表时生成一次（键控复用），每 2 秒只原地更新文本与按钮状态，
  // 不整块重绘 —— 否则点按钮的瞬间 DOM 被换掉，点击就会丢。
  var projEls = {};       // 项目名 -> {root, refs}
  var projSig = null;
  var projRows = [];
  var projFilter = "";
  var pending = {};       // 项目名 -> 正在执行的动作
  var logTarget = null;   // 正在看日志的项目名
  var logTimer = null;
  var confirmCtx = null;  // 待确认的 {name, action}
  var toastTimer = null;
  var editName = null;    // 正在编辑哪张卡片（编辑期间 updateTile 会跳过它）
  var editBusy = false;

  var ACTION_LABEL = { start: "启动", stop: "停止", restart: "重启" };
  // 按钮随状态显隐：运行中 → 重启/停止；停着 → 启动
  var ACTION_MATRIX = {
    running: { start: false, restart: true, stop: true },
    starting: { start: false, restart: true, stop: true },
    stopping: { start: false, restart: true, stop: true },
    failed: { start: true, restart: true, stop: false },
    stopped: { start: true, restart: false, stop: false },
    missing: { start: false, restart: false, stop: false },
    other: { start: true, restart: true, stop: true }
  };

  function apiUrl(path, params) {
    // 登录态走 HttpOnly Cookie，URL 里不再带密码；这里只拼调用方给出的查询参数
    var qs = [];
    Object.keys(params || {}).forEach(function (key) {
      qs.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
    });
    return path + (qs.length ? "?" + qs.join("&") : "");
  }

  // 会话过期/被登出时，把页面送回登录页（轮询、日志、启停都会经过这里）
  function gotoLogin() {
    window.location.replace("/login");
  }

  function errText(err) {
    return err && err.message ? err.message : String(err || "网络错误");
  }

  function kindLabel(p) {
    if (p.kind === "docker") { return "容器"; }
    if (p.kind === "port") { return "端口探测"; }
    return p.scope === "system" ? "系统服务" : "用户服务";
  }

  function makeEl(tag, cls) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    return node;
  }

  function tileSkeleton(p) {
    // 骨架里的动态文本都用占位符，真正内容在 updateTile 里写（textContent，不拼 HTML）。
    // 首行只放「状态点 + 名称 + 编辑笔」：类型标签、端口、只读标记都挪到第二行，
    // 否则窄卡片里名称会被这些元素挤成「Clash (mih…」「WorkBuddy …」。
    return "<div class='proj-top'>" +
      "<span class='dot'></span>" +
      "<span class='proj-title'></span>" +
      "<button class='proj-edit-btn' type='button' data-action='edit' title='编辑这张卡片'>✎</button>" +
      "</div>" +
      "<p class='proj-desc'></p>" +
      "<div class='proj-meta'>" +
      "<span class='tag'></span>" +
      "<span class='proj-state'></span>" +
      "<span class='proj-enabled' hidden>· 自启</span>" +
      "<span class='proj-uptime'></span>" +
      "<span class='proj-res'></span>" +
      "<span class='proj-meta-right'>" +
      "<a class='proj-port' target='_blank' rel='noopener' hidden></a>" +
      "<span class='proj-readonly' hidden>只读</span>" +
      "</span>" +
      "</div>" +
      "<p class='proj-hint' hidden></p>" +
      "<div class='proj-acts'>" +
      "<button class='proj-act act-start' type='button' data-action='start'>启动</button>" +
      "<button class='proj-act act-restart' type='button' data-action='restart'>重启</button>" +
      "<button class='proj-act act-stop' type='button' data-action='stop'>停止</button>" +
      "<button class='proj-act ghost' type='button' data-action='logs'>日志</button>" +
      "</div>" +
      "<form class='proj-edit' hidden>" +
      "<label class='pe-label'>名称<input class='pe-input' name='title' maxlength='60' placeholder='卡片上显示的名字'></label>" +
      "<label class='pe-label'>说明<input class='pe-input' name='desc' maxlength='200' placeholder='一句话说明'></label>" +
      "<label class='pe-label'>分组<input class='pe-input' name='group' maxlength='40' list='proj-group-options' placeholder='选已有的或直接输新组名'></label>" +
      "<div class='pe-pair'>" +
      "<label class='pe-label'>端口<input class='pe-input' name='port' type='text' inputmode='numeric' placeholder='留空则不显示'></label>" +
      "<label class='pe-label'>路径<input class='pe-input' name='path' maxlength='60' placeholder='/ui'></label>" +
      "</div>" +
      "<label class='pe-check'><input type='checkbox' name='link'> 端口可点开</label>" +
      "<p class='pe-target'></p>" +
      "<div class='pe-acts'>" +
      "<button class='proj-act pe-save' type='submit'>保存</button>" +
      "<button class='proj-act ghost' type='button' data-edit='cancel'>取消</button>" +
      "<button class='proj-act ghost pe-drop' type='button' data-edit='unregister' hidden>取消登记</button>" +
      "</div>" +
      "</form>";
  }

  function buildTiles(projects) {
    var host = $("proj-groups");
    if (!host) { return; }
    host.innerHTML = "";
    projEls = {};
    var group = null, grid = null;
    projects.forEach(function (p) {
      if (!group || group.getAttribute("data-group") !== p.group) {
        group = makeEl("div", "proj-group");
        group.setAttribute("data-group", p.group);
        var title = makeEl("p", "proj-group-title");
        title.textContent = p.group;
        grid = makeEl("div", "proj-grid");
        group.appendChild(title);
        group.appendChild(grid);
        host.appendChild(group);
      }
      var root = makeEl("div", "proj-tile");
      root.setAttribute("data-name", p.name);
      root.innerHTML = tileSkeleton(p);
      var q = function (sel) { return root.querySelector(sel); };
      root.querySelector(".proj-title").textContent = p.title || p.name;
      root.querySelector(".tag").textContent = kindLabel(p);
      var desc = q(".proj-desc");
      desc.textContent = p.desc || "";
      desc.hidden = !p.desc;
      var form = q(".proj-edit");
      projEls[p.name] = {
        root: root,
        grid: grid,
        dot: q(".dot"),
        state: q(".proj-state"),
        enabled: q(".proj-enabled"),
        uptime: q(".proj-uptime"),
        res: q(".proj-res"),
        hint: q(".proj-hint"),
        port: q(".proj-port"),
        readonly: q(".proj-readonly"),
        start: q(".act-start"),
        restart: q(".act-restart"),
        stop: q(".act-stop"),
        logs: q(".ghost"),
        form: form,
        f: {
          title: form.querySelector("[name=title]"),
          desc: form.querySelector("[name=desc]"),
          group: form.querySelector("[name=group]"),
          port: form.querySelector("[name=port]"),
          path: form.querySelector("[name=path]"),
          link: form.querySelector("[name=link]")
        },
        target: form.querySelector(".pe-target"),
        drop: form.querySelector(".pe-drop")
      };
      grid.appendChild(root);
    });
  }

  function updateTile(p) {
    var refs = projEls[p.name];
    if (!refs) { return; }
    // 正在编辑这张卡时不要覆盖它的内容，否则用户打字打到一半就被刷掉
    if (editName === p.name) { return; }
    // 名称/说明也要在这里跟着刷新：它们只在建表时写过一次的话，
    // 页面内改完名字要等下次重建 DOM 才显示（实测踩到过）
    var titleEl = refs.root.querySelector(".proj-title");
    if (titleEl) { titleEl.textContent = p.title || p.name; }
    var tagEl = refs.root.querySelector(".tag");
    if (tagEl) { tagEl.textContent = kindLabel(p); }
    var descEl = refs.root.querySelector(".proj-desc");
    if (descEl) {
      descEl.textContent = p.desc || "";
      descEl.hidden = !p.desc;
    }
    var stateClass = { running: "up pulse", starting: "starting", stopping: "starting",
                       failed: "failed", stopped: "", missing: "missing", other: "" };
    refs.dot.className = "dot " + (stateClass[p.state] || "");
    refs.state.textContent = p.state_text || (p.up ? "运行中" : "已停止");
    refs.state.className = "proj-state " + (p.state || "");
    refs.enabled.hidden = !(p.enabled === "enabled" || p.enabled === "enabled-runtime");

    refs.uptime.textContent = p.uptime_sec != null && p.up ? "· " + fmtUptime(p.uptime_sec) : "";
    refs.res.textContent = p.cpu != null
      ? "· CPU " + p.cpu.toFixed(1) + "% · " + fmtSize(p.rss) : "";

    // 端口：可点开的项目页面给链接（加 ↗），纯 TCP 端口（redis/postgres 这类）只显示端口号
    if (p.port && p.link) {
      refs.port.textContent = ":" + p.port + " ↗";
      refs.port.href = "http://" + window.location.hostname + ":" + p.port + (p.path || "");
      refs.port.title = "打开 " + (p.title || p.name) + " 的页面";
      refs.port.hidden = false;
      refs.port.classList.remove("plain");
    } else if (p.port) {
      refs.port.textContent = ":" + p.port;
      refs.port.removeAttribute("href");
      refs.port.title = "纯 TCP 端口，没有网页界面";
      refs.port.hidden = false;
      refs.port.classList.add("plain");
    } else {
      refs.port.hidden = true;
    }

    var hint = p.hint || p.why || "";
    refs.hint.textContent = hint;
    refs.hint.hidden = !hint;
    refs.hint.classList.toggle("warn", !!p.hint);
    refs.hint.classList.toggle("muted", !p.hint);

    var matrix = ACTION_MATRIX[p.state] || ACTION_MATRIX.other;
    var busy = !!pending[p.name];
    [refs.start, refs.restart, refs.stop].forEach(function (btn) {
      if (!btn) { return; }
      var action = btn.getAttribute("data-action");
      // 不可用的动作直接隐藏，不留一个灰按钮占位置（状态文案已经说明运行中/已停止）。
      // 正在执行时保持可见但禁用，让用户看到"这一下点到了"。
      var usable = !!matrix[action] && p.manageable;
      btn.hidden = !usable;
      btn.disabled = !usable || busy;
      btn.title = "";
      if (!p.manageable) { btn.title = p.why || "该项目不可从这里操作"; }
      else if (busy) { btn.title = "正在" + ACTION_LABEL[pending[p.name]] + "…"; }
    });
    refs.root.classList.toggle("busy", busy);
    refs.root.classList.toggle("readonly", !p.manageable);
    if (p.kind === "port" || p.state === "missing") {
      refs.logs.disabled = true;
      refs.logs.title = p.kind === "port" ? "端口探测项目没有日志" : "单元不存在，没有日志可读";
    } else {
      refs.logs.disabled = false;
      refs.logs.title = "";
    }
    refs.readonly.hidden = p.manageable;
    refs.readonly.title = p.why || "";
  }

  function applyFilter() {
    var words = projFilter.toLowerCase().split(/\s+/).filter(Boolean);
    var groups = document.querySelectorAll("#proj-groups .proj-group");
    groups.forEach(function (g) {
      var visible = 0;
      g.querySelectorAll(".proj-tile").forEach(function (tile) {
        var name = tile.getAttribute("data-name");
        var row = findRow(name);
        var hay = (name + " " + ((row && (row.title + " " + row.desc + " " + row.group)) || "")).toLowerCase();
        var hit = !words.length || words.every(function (w) { return hay.indexOf(w) >= 0; });
        tile.classList.toggle("filtered", !hit);
        if (hit) { visible++; }
      });
      g.classList.toggle("filtered", !visible);
    });
  }

  function renderProjects(projects) {
    projRows = projects || [];
    if (!projRows.length) {
      $("proj-groups").innerHTML = "<p class='proj-empty'>登记表（projects.json）里没有项目</p>";
      return;
    }
    // 名单或分组变了才重建 DOM；平时只原地更新
    var sig = projRows.map(function (p) { return p.name + "@" + p.group; }).join("|");
    if (sig !== projSig) {
      projSig = sig;
      var wasEditing = editName;
      editName = null;                 // 先清掉，避免新 DOM 里残留旧引用
      buildTiles(projRows);
      applyFilter();
      if (wasEditing && projEls[wasEditing]) { openEdit(wasEditing); }  // 重建后把编辑框接回来
    }
    projRows.forEach(updateTile);
    renderGroupOptions(lastStats && lastStats.groups);

    var up = projRows.filter(function (p) { return p.up; }).length;
    var note = "运行 " + up + " · 共 " + projRows.length;
    if (lastStats && lastStats.control === false) {
      note += " · 只读模式";
    } else if (lastStats && lastStats.helper && !lastStats.helper.ready) {
      note += " · 系统服务只读（未装助手）";
    }
    setText("proj-note", note);
    // 登录面板里才显示「退出登录」；无密码部署时后端会带 auth=false
    var tools = $("login-tools");
    if (tools) { tools.hidden = !(lastStats && lastStats.auth); }
    var auto = projRows.filter(function (p) { return p.auto; }).length;
    var gone = (lastStats && lastStats.gone_projects) || [];
    var foot = ["登记表见 projects.json，改完即时生效不用重启"];
    if (auto) { foot.unshift("「自动发现」分组里有 " + auto + " 个自动发现的自装服务"); }
    if (gone.length) {
      foot.push("已隐藏 " + gone.length + " 个本机已不存在的条目（" +
        gone.map(function (g) { return g.name; }).join("、") + "）");
    }
    setText("proj-foot", foot.join(" · "));
  }

  function findRow(name) {
    for (var i = 0; i < projRows.length; i++) {
      if (projRows[i].name === name) { return projRows[i]; }
    }
    return null;
  }

  /* ---------- 项目操作：启停 / 确认 / 日志 / 提示 ---------- */

  function toast(text, kind) {
    var t = $("toast");
    if (!t) { return; }
    t.textContent = text;
    t.className = "toast " + (kind || "");
    t.hidden = false;
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { t.hidden = true; }, 4500);
  }

  function doAction(name, action) {
    var row = findRow(name);
    if (!row) { return; }
    if (!row.manageable) {
      if (row.kind === "systemd" && row.scope === "system") { openHelperModal(); }
      else { toast(row.why || "该项目不可从这里操作", "err"); }
      return;
    }
    // 关键项目（sysboard 自己、DSH）停止/重启前要求输入名字确认
    if (row.critical && action !== "start") {
      askConfirm(name, action);
      return;
    }
    sendAction(name, action, false);
  }

  function sendAction(name, action, confirmed) {
    var body = JSON.stringify({ name: name, action: action, confirm: !!confirmed });
    pending[name] = action;
    projRows.forEach(updateTile);
    toast((findRow(name) && findRow(name).title || name) + " · 正在" + ACTION_LABEL[action] + "…", "info");
    fetch(apiUrl("/api/ctl"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    }).then(function (res) {
      if (res.status === 401) { gotoLogin(); return { ok: false, status: 401, body: {} }; }
      return res.json().then(function (j) { return { ok: res.ok, status: res.status, body: j }; });
    }).then(function (r) {
      delete pending[name];
      projRows.forEach(updateTile);
      if (r.status === 428 || (r.body && r.body.confirm_required)) {
        askConfirm(name, action);          // 服务端也要求确认（比如手动 curl 触发）
        return;
      }
      toast((r.body && r.body.message) || (r.ok ? "完成" : "失败"), r.ok ? "ok" : "err");
      refreshSoon();
    }).catch(function (err) {
      delete pending[name];
      projRows.forEach(updateTile);
      toast("请求失败 · " + errText(err), "err");
    });
  }

  function refreshSoon() {
    poll();
    setTimeout(poll, 1500);   // 启停后状态缓存已作废，多拉一次确保卡片跟上
  }

  function askConfirm(name, action) {
    var row = findRow(name);
    confirmCtx = { name: name, action: action };
    setText("confirm-text",
      "确定要" + ACTION_LABEL[action] + "「" + ((row && row.title) || name) + "」吗？" +
      (action === "stop" ? " 停它可能让你连不上机器或丢掉当前页面。" : "") +
      "输入项目名以确认。");
    var input = $("confirm-input");
    if (input) { input.value = ""; }
    var ok = $("confirm-ok");
    if (ok) { ok.disabled = true; }
    $("confirm-modal").hidden = false;
    if (input) { input.focus(); }
  }

  function closeConfirm() {
    $("confirm-modal").hidden = true;
    confirmCtx = null;
  }

  function bindProjects() {
    var host = $("proj-groups");
    if (host) {
      host.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("button[data-action]") : null;
        if (!btn) { return; }
        var tile = btn.closest(".proj-tile");
        if (!tile) { return; }
        var name = tile.getAttribute("data-name");
        var action = btn.getAttribute("data-action");
        if (action === "logs") { openLogs(name); return; }
        if (action === "edit") { openEdit(name); return; }
        doAction(name, action);
      });
      // 编辑表单的取消 / 取消登记
      host.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("button[data-edit]") : null;
        if (!btn) { return; }
        var tile = btn.closest(".proj-tile");
        if (!tile) { return; }
        var name = tile.getAttribute("data-name");
        if (btn.getAttribute("data-edit") === "cancel") { closeEdit(); return; }
        unregisterProject(name);
      });
      // 表单提交保存
      host.addEventListener("submit", function (e) {
        var form = e.target.closest ? e.target.closest("form.proj-edit") : null;
        if (!form) { return; }
        e.preventDefault();
        var tile = form.closest(".proj-tile");
        if (tile) { saveEdit(tile.getAttribute("data-name"), form); }
      });
    }
    var filter = $("proj-filter");
    if (filter) {
      filter.addEventListener("input", function () {
        projFilter = filter.value || "";
        applyFilter();
      });
    }
    // 确认弹窗
    var input = $("confirm-input");
    if (input) {
      input.addEventListener("input", function () {
        var ok = $("confirm-ok");
        if (ok) { ok.disabled = !confirmCtx || input.value.trim() !== confirmCtx.name; }
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && confirmCtx && input.value.trim() === confirmCtx.name) {
          var ctx = confirmCtx;
          closeConfirm();
          sendAction(ctx.name, ctx.action, true);
        }
      });
    }
    var ok = $("confirm-ok");
    if (ok) {
      ok.addEventListener("click", function () {
        if (!confirmCtx) { return; }
        var ctx = confirmCtx;
        closeConfirm();
        sendAction(ctx.name, ctx.action, true);
      });
    }
    var cancel = $("confirm-cancel");
    if (cancel) { cancel.addEventListener("click", closeConfirm); }
    // 日志弹窗
    var logClose = $("log-modal-close");
    if (logClose) { logClose.addEventListener("click", closeLogs); }
    var logRefresh = $("log-refresh");
    if (logRefresh) { logRefresh.addEventListener("click", loadLogs); }
    // 助手弹窗
    var helperClose = $("helper-modal-close");
    if (helperClose) { helperClose.addEventListener("click", closeHelperModal); }
  }

  function openHelperModal() {
    var cmds = $("helper-cmds");
    var install = (lastStats && lastStats.helper && lastStats.helper.install) || [];
    if (cmds) { cmds.textContent = install.join("\n") || "（见 README 的「系统级服务控制」一节）"; }
    var state = $("helper-state");
    if (state) {
      state.textContent = lastStats && lastStats.helper && lastStats.helper.ready
        ? "助手已安装，系统级服务可以操作了。" : "当前未检测到助手，系统级项目是只读的。装完不用重启面板，一分钟内自动生效。";
    }
    $("helper-modal").hidden = false;
  }

  function closeHelperModal() {
    $("helper-modal").hidden = true;
  }

  function openLogs(name) {
    var row = findRow(name);
    logTarget = name;
    setText("log-title", ((row && row.title) || name) + " · 日志");
    $("log-modal").hidden = false;
    var body = $("log-body");
    if (body) { body.textContent = "加载中…"; }
    loadLogs();
    if (logTimer) { clearInterval(logTimer); }
    logTimer = setInterval(function () {
      var box = $("log-follow");
      if (logTarget && box && box.checked) { loadLogs(); }
    }, LOG_MS);
  }

  function closeLogs() {
    logTarget = null;
    if (logTimer) { clearInterval(logTimer); }
    logTimer = null;
    $("log-modal").hidden = true;
  }

  function loadLogs() {
    if (!logTarget) { return; }
    var name = logTarget;
    fetch(apiUrl("/api/logs", { name: name, tail: LOG_TAIL }), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 401) { gotoLogin(); }
        return res.json();
      })
      .then(function (j) {
        if (name !== logTarget) { return; }   // 弹窗已经切到别的项目了
        var body = $("log-body");
        var note = $("log-note");
        if (!body) { return; }
        var stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
        body.textContent = (j && j.text) || "（无内容）";
        setText("log-note", (j && j.source ? "来源：" + j.source : "") +
          (j && !j.ok ? " · " + (j.message || "读取失败") : ""));
        note.classList.toggle("error", !!(j && !j.ok));
        if (stick) { body.scrollTop = body.scrollHeight; }
      })
      .catch(function (err) {
        if (name !== logTarget) { return; }
        var body = $("log-body");
        if (body) { body.textContent = "读取失败 · " + errText(err); }
      });
  }

  /* ---------- 卡片编辑 / 分组管理 ---------- */

  function renderGroupOptions(groups) {
    // 分组候选：stats 里显式登记的分组（含还没项目的空组）+ 正在用的分组
    var list = $("proj-group-options");
    if (!list) { return; }
    var seen = {};
    var all = [];
    (groups || []).concat(projRows.map(function (p) { return p.group; })).forEach(function (g) {
      if (g && !seen[g]) { seen[g] = 1; all.push(g); }
    });
    list.innerHTML = all.map(function (g) {
      return "<option value='" + esc(g) + "'></option>";
    }).join("");
  }

  function openEdit(name) {
    var refs = projEls[name];
    var row = findRow(name);
    if (!refs || !row) { return; }
    if (editName && editName !== name) { closeEdit(); }
    editName = name;
    var f = refs.f;
    f.title.value = row.title || row.name;
    f.desc.value = row.desc || "";
    f.group.value = row.group || "";
    f.port.value = row.port == null ? "" : row.port;
    f.path.value = row.path || "";
    f.link.checked = !!row.link;
    // 明确告诉用户改不了什么：目标（单元/容器）是这张卡的身份，改了就对不上了
    var target = row.kind === "systemd" ? (row.scope === "system" ? "系统级单元 " : "用户级单元 ") + row.unit
      : row.kind === "docker" ? "容器 " + row.container : "端口探测";
    refs.target.textContent = "实际目标：" + target + "（只读，改显示名不影响它）";
    refs.drop.hidden = !!row.auto;      // 自动发现的项目本来就没登记，谈不上取消登记
    refs.form.hidden = false;
    refs.root.classList.add("editing");
    f.title.focus();
    f.title.select();
  }

  function closeEdit() {
    if (!editName) { return; }
    var refs = projEls[editName];
    if (refs) {
      refs.form.hidden = true;
      refs.root.classList.remove("editing");
    }
    editName = null;
  }

  function pushEdit(payload, done) {
    if (editBusy) { return; }
    editBusy = true;
    fetch(apiUrl("/api/projects"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (res) {
      if (res.status === 401) { gotoLogin(); return { ok: false, body: {} }; }
      return res.json().then(function (j) { return { ok: res.ok, body: j }; });
    }).then(function (r) {
      editBusy = false;
      if (r.status === 401) { return; }
      toast((r.body && r.body.message) || (r.ok ? "完成" : "失败"), r.ok ? "ok" : "err");
      if (r.ok && done) { done(); }
      if (r.ok) { refreshSoon(); }
    }).catch(function (err) {
      editBusy = false;
      toast("请求失败 · " + errText(err), "err");
    });
  }

  function saveEdit(name, form) {
    var data = new FormData(form);
    pushEdit({
      action: "save",
      name: name,
      title: data.get("title"),
      desc: data.get("desc"),
      group: data.get("group"),
      port: (data.get("port") || "").trim(),
      path: (data.get("path") || "").trim(),
      link: !!data.get("link")
    }, closeEdit);
  }

  function unregisterProject(name) {
    var row = findRow(name);
    if (!row) { return; }
    pushEdit({ action: "delete", name: name }, function () {
      closeEdit();
      toast("已取消登记「" + (row.title || name) + "」", "ok");
    });
  }

  function openGroupModal() {
    var m = $("group-modal");
    if (!m) { return; }
    renderGroupModal();
    m.hidden = false;
  }

  function closeGroupModal() {
    var m = $("group-modal");
    if (m) { m.hidden = true; }
  }

  function renderGroupModal() {
    var body = $("group-modal-body");
    if (!body) { return; }
    var count = {};
    projRows.forEach(function (p) { count[p.group] = (count[p.group] || 0) + 1; });
    var names = [];
    var seen = {};
    ((lastStats && lastStats.groups) || []).concat(projRows.map(function (p) { return p.group; }))
      .forEach(function (g) { if (g && !seen[g]) { seen[g] = 1; names.push(g); } });
    if (!names.length) {
      body.innerHTML = "<p class='modal-group'>还没有分组。在下面新建一个，或者直接在卡片上用 ✎ 填一个新组名。</p>";
      return;
    }
    body.innerHTML = names.map(function (g) {
      var n = count[g] || 0;
      return "<div class='modal-row' data-group='" + esc(g) + "'>" +
        "<input class='gm-input' value='" + esc(g) + "' maxlength='40' aria-label='分组名'>" +
        "<span class='gm-count'>" + n + " 个项目</span>" +
        "<button class='modal-act' type='button' data-gm='rename'>改名</button>" +
        "<button class='modal-act danger' type='button' data-gm='delete'" +
        (n ? " disabled title='组里还有项目，先移走再删'" : "") + ">删除</button>" +
        "</div>";
    }).join("");
  }

  function bindGroupModal() {
    var open = $("group-manage");
    if (open) { open.addEventListener("click", openGroupModal); }
    var close = $("group-modal-close");
    if (close) { close.addEventListener("click", closeGroupModal); }
    var backdrop = $("group-modal");
    if (backdrop) {
      backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) { closeGroupModal(); }
      });
    }
    var body = $("group-modal-body");
    if (body) {
      body.addEventListener("click", function (e) {
        var btn = e.target.closest ? e.target.closest("button[data-gm]") : null;
        if (!btn) { return; }
        var row = btn.closest(".modal-row");
        var from = row.getAttribute("data-group");
        if (btn.getAttribute("data-gm") === "delete") {
          pushEdit({ action: "group_delete", group: from }, renderGroupModal);
          return;
        }
        var to = row.querySelector(".gm-input").value.trim();
        if (!to || to === from) { toast("分组名没变", "info"); return; }
        pushEdit({ action: "group_rename", group: from, to: to }, renderGroupModal);
      });
    }
    var addBtn = $("group-add-btn");
    var addInput = $("group-add-input");
    var add = function () {
      var name = (addInput && addInput.value || "").trim();
      if (!name) { return; }
      pushEdit({ action: "group_add", group: name }, function () {
        if (addInput) { addInput.value = ""; }
        renderGroupModal();
      });
    };
    if (addBtn) { addBtn.addEventListener("click", add); }
    if (addInput) {
      addInput.addEventListener("keydown", function (e) { if (e.key === "Enter") { add(); } });
    }
  }

  function update(stats) {
    push(hist.t, timeLabel(stats.time));
    push(hist.cpu, +stats.cpu.percent.toFixed(1));
    push(hist.mem, +stats.mem.percent.toFixed(1));
    push(hist.read, Math.round(stats.disk.read_bps));
    push(hist.write, Math.round(stats.disk.write_bps));
    var primary = primaryNet(stats.net);
    if (primary) {
      push(hist.down, Math.round(primary.data.down_bps));
      push(hist.up, Math.round(primary.data.up_bps));
    } else {
      push(hist.down, 0);
      push(hist.up, 0);
    }
    push(hist.temp, stats.temp == null ? null : +stats.temp.toFixed(1));

    // 单张卡片的数据异常不该让整页停更：逐张隔离，第一处错误交给页脚显示
    renderError = null;
    var cards = [
      ["主机", function () { renderHost(stats.host); }],
      ["处理器", function () { renderCpu(stats.cpu); }],
      ["内存", function () { renderMem(stats.mem, stats.swap); }],
      ["磁盘", function () { renderDisk(stats.disk); }],
      ["网络", function () { renderNet(stats.net, stats.net_totals, stats.net_total, stats.host); }],
      ["温度", function () { renderTemp(stats.temp, (stats.disk || {}).drives); }],
      ["进程", function () { renderProcTable("proc", stats.procs); }],
      ["内存进程", function () { renderProcTable("memproc", stats.mem_procs); }],
      ["项目", function () { renderProjects(stats.projects || []); }]
    ];
    cards.forEach(function (card) {
      try {
        card[1]();
      } catch (err) {
        if (!renderError) {
          renderError = card[0] + " · " + (err && err.message ? err.message : err);
        }
      }
    });
    // 磁盘管理弹窗若开着，跟着数据一起重渲染，新插入的硬盘会实时出现
    var dm = $("disk-modal");
    if (dm && !dm.hidden) { renderDiskModal(); }
    // 分组弹窗同理：新建的空分组要靠 stats.groups 才看得见，
    // 只在提交后重绘一次的话会显示旧列表（实测漏过）
    var gm = $("group-modal");
    if (gm && !gm.hidden) { renderGroupModal(); }
  }

  /* ---------- 轮询与时钟 ---------- */

  function setStatus(text, isError) {
    var el = $("conn-status");
    if (!el) { return; }
    el.textContent = text;
    el.classList.toggle("error", !!isError);
  }

  function poll() {
    fetch(apiUrl("/api/stats"), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 401) { gotoLogin(); return null; }
        if (res.status === 503) {
          // 服务刚重启、采集线程还没产出第一份快照，属于正常启动窗口
          setStatus("启动中…", false);
          return null;
        }
        if (!res.ok) { throw new Error("HTTP " + res.status); }
        return res.json();
      })
      .then(function (stats) {
        if (!stats) { return; }
        lastStats = stats;
        update(stats);
        if (stats.build && stats.build !== pageBuild) {
          // 指纹对不上（含页面根本没有指纹的旧 HTML）= 这个标签页是旧页面
          setStatus("页面已过期，请强制刷新（Ctrl+Shift+R）", true);
        } else if (renderError) {
          // 渲染出错和网络中断是两回事，页脚要说实情，别让用户误判成连接问题
          setStatus("渲染异常 · " + renderError, true);
        } else {
          setStatus("已连接", false);
        }
      })
      .catch(function (err) {
        // 只有网络失败会走到这里，把具体原因显示出来便于判断
        var reason = err && err.message ? err.message : "网络错误";
        setStatus("连接中断 · " + reason, true);
      });
  }

  function tickClock() {
    var now = new Date();
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    setText("clock", pad(now.getHours()) + ":" + pad(now.getMinutes()) + ":" + pad(now.getSeconds()));
    setText("clock-date", now.toLocaleDateString("zh-CN",
      { year: "numeric", month: "long", day: "numeric", weekday: "long" }));
  }

  function doLogout() {
    // 带上 JSON 头与空体：服务端对退出也做同源校验（和启停接口同一套规矩）
    fetch("/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function () { gotoLogin(); });
  }

  function bindSorting() {
    document.querySelectorAll(".proc-table th.sortable").forEach(function (th) {
      th.addEventListener("click", function () {
        var table = th.closest("table");
        var bodyId = th.closest("section").querySelector("tbody").id;
        var which = bodyId === "memproc-body" ? "memproc" : "proc";
        var key = th.getAttribute("data-key");
        var state = sorts[which];
        if (state.key === key) {
          state.dir = -state.dir;
        } else {
          state.key = key;
          state.dir = (key === "name" || key === "user") ? 1 : -1;
        }
        table.querySelectorAll("th.sortable").forEach(function (h) { h.classList.remove("active", "asc"); });
        th.classList.add("active");
        if (state.dir === 1) { th.classList.add("asc"); }
        if (lastStats) { renderProcTable(which, lastStats[state.source]); }
      });
    });
  }

  /* ---------- 磁盘管理弹窗 ---------- */

  function openDiskModal() {
    var m = $("disk-modal");
    if (m) {
      m.hidden = false;
      renderDiskModal();
    }
  }

  function closeDiskModal() {
    var m = $("disk-modal");
    if (m) { m.hidden = true; }
  }

  function renderDiskModal() {
    var body = $("disk-modal-body");
    if (!body) { return; }
    var d = lastStats && lastStats.disk;
    if (!d) {
      setText("disk-modal-note", "还没有拿到磁盘数据，稍候…");
      return;
    }
    var parts = [];
    if ((d.available || []).length) {
      parts.push("<p class='modal-group'>未挂载</p>");
      d.available.forEach(function (a) {
        parts.push("<div class='modal-row'><span class='modal-dev'>" + esc(a.dev) +
          "</span><span class='modal-fs'>" + esc(a.fstype) +
          "</span><span class='modal-size'>" + esc(a.label || "无卷标") + " · " + fmtSize(a.size) +
          "</span><button class='modal-act' type='button' data-action='mount' data-device='" +
          esc(a.dev.replace("/dev/", "")) + "'>挂载</button></div>");
      });
    } else {
      parts.push("<p class='modal-group'>没有检测到未挂载的分区（插入硬盘后会自动挂载）</p>");
    }
    var removable = (d.mounts || []).filter(function (m) { return m.removable; });
    if (removable.length) {
      parts.push("<p class='modal-group'>可卸载（拔出硬盘前先卸载）</p>");
      removable.forEach(function (m) {
        parts.push("<div class='modal-row'><span class='modal-dev'>" + esc(m.dev) +
          "</span><span class='modal-fs'>" + esc(m.fstype) +
          "</span><span class='modal-size'>" + esc(m.mount) +
          "</span><button class='modal-act danger' type='button' data-action='unmount' data-device='" +
          esc(m.dev.replace("/dev/", "")) + "'>卸载</button></div>");
      });
    }
    body.innerHTML = parts.join("");
    setText("disk-modal-note", "");
  }

  function diskAction(action, device, btn) {
    var note = $("disk-modal-note");
    if (btn) { btn.disabled = true; }
    if (note) { note.textContent = "正在" + (action === "mount" ? "挂载" : "卸载") + " /dev/" + device + " …"; }
    // 页面带 token 访问时（?token=xxx）把 token 一起带给接口
    fetch(apiUrl("/api/disk"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, device: device })
    }).then(function (res) {
      if (res.status === 401) { gotoLogin(); return { ok: false, body: {} }; }
      return res.json().then(function (j) { return { ok: res.ok, body: j }; });
    }).then(function (r) {
      if (note) {
        note.textContent = (r.body && r.body.message) || (r.ok ? "完成" : "失败");
        note.classList.toggle("error", !r.ok);
      }
      poll();               // 立刻刷新，不等下个轮询周期
      renderDiskModal();    // 重新拉取按钮状态
    }).catch(function (err) {
      if (note) {
        note.textContent = "请求失败 · " + (err && err.message ? err.message : "网络错误");
        note.classList.add("error");
      }
    });
  }

  function bindDiskModal() {
    var btn = $("disk-manage");
    if (btn) { btn.addEventListener("click", openDiskModal); }
    var close = $("disk-modal-close");
    if (close) { close.addEventListener("click", closeDiskModal); }
    var backdrop = $("disk-modal");
    if (backdrop) {
      backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) { closeDiskModal(); }
      });
    }
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeDiskModal();
        closeConfirm();
        closeLogs();
        closeHelperModal();
        closeGroupModal();
        closeEdit();
      }
    });
    var body = $("disk-modal-body");
    if (body) {
      body.addEventListener("click", function (e) {
        var t = e.target.closest ? e.target.closest("button[data-action]") : null;
        if (t) { diskAction(t.getAttribute("data-action"), t.getAttribute("data-device"), t); }
      });
    }
  }

  /* ---------- 启动 ---------- */

  // 页脚版本号来自本页面自身携带的构建指纹，与服务器当前构建（随 API 下发）
  // 比对即可发现"标签页跑的是旧页面"，见 poll() 里的过期提示。
  var pageBuild = null;

  function showBuild() {
    var meta = document.querySelector('meta[name="build"]');
    var el = $("build-id");
    if (!meta) { return; }
    pageBuild = meta.getAttribute("content") || null;
    if (el) { el.textContent = pageBuild || "—"; }
  }

  initCharts();
  bindSorting();
  bindDiskModal();
  bindProjects();
  bindGroupModal();
  // 开了登录才显示退出按钮（stats.auth 由后端下发）
  var logoutBtn = $("logout");
  if (logoutBtn) { logoutBtn.addEventListener("click", doLogout); }
  showBuild();
  tickClock();
  // 自动化/调试入口：?diskmodal=1 磁盘弹窗，?logmodal=<项目名> 日志弹窗，
  // ?helpermodal=1 助手安装弹窗，?confirmmodal=<项目名> 关键操作确认弹窗，
  // ?card=<卡片id> 只显示某一张卡（离线核对单张卡片用，见 README）
  var search = window.location.search;
  var cardMatch = search.match(/[?&]card=([A-Za-z0-9_-]+)/);
  if (cardMatch) {
    document.querySelectorAll("main.grid > section.card").forEach(function (section) {
      if (section.id !== cardMatch[1]) { section.style.display = "none"; }
    });
  }
  if (search.indexOf("diskmodal=1") >= 0) { openDiskModal(); }
  var logMatch = search.match(/[?&]logmodal=([^&]+)/);
  if (logMatch) {
    var wantLog = decodeURIComponent(logMatch[1]);
    setTimeout(function () { openLogs(wantLog); }, 300);   // 等第一份 stats 到位再开
  }
  var confirmMatch = search.match(/[?&]confirmmodal=([^&]+)/);
  if (confirmMatch) {
    var wantConfirm = decodeURIComponent(confirmMatch[1]);
    setTimeout(function () { askConfirm(wantConfirm, "stop"); }, 300);
  }
  if (search.indexOf("helpermodal=1") >= 0) {
    setTimeout(openHelperModal, 300);
  }
  var editMatch = search.match(/[?&]editmodal=([^&]+)/);
  if (editMatch) {
    var wantEdit = decodeURIComponent(editMatch[1]);
    setTimeout(function () { openEdit(wantEdit); }, 500);
  }
  if (search.indexOf("groupmodal=1") >= 0) {
    setTimeout(openGroupModal, 500);
  }
  // 弹窗背板点击 / 新弹窗的关闭按钮
  ["log-modal", "confirm-modal", "helper-modal"].forEach(function (id) {
    var backdrop = $(id);
    if (backdrop) {
      backdrop.addEventListener("click", function (e) {
        if (e.target === backdrop) { $(id).hidden = true; logTarget = null; confirmCtx = null; }
      });
    }
  });
  setInterval(tickClock, 1000);
  poll();
  setInterval(poll, POLL_MS);
  window.addEventListener("resize", function () {
    Object.keys(charts).forEach(function (k) { charts[k].resize(); });
  });
})();
