# SysBoard · 中文系统看板

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

SysBoard 是一个轻量级中文系统监控看板，最初为一台单板机（树莓派）自研。后端只依赖 Python 标准库和 psutil，前端使用本地化的 ECharts，界面采用 Claude 风格主题，无需外网即可运行。

除了常规的 CPU、内存、磁盘、网络与温度图表，它还有一块**项目管理面板**：把本机由 systemd（用户级/系统级）、Docker 管理的项目与纯端口探测目标统一列出，可在网页上启动、重启、停止、查看日志，并直达项目自身的 Web 页面。

> **隐私说明**：本项目仓库只包含通用代码与示例配置。真实运行时的 projects.json、访问密码、会话签名密钥与日志均不会提交。

## 功能特性

- **系统监控**：CPU（含每核）、内存、Swap、根分区、网络实时速率与开机累计流量、每块物理盘读写/忙碌占比/SMART 温度、进程 Top 榜
- **项目管理**：自动发现 systemd 单元与 Docker 容器；支持自定义分组、显示名、说明、端口、直达链接与排序；启停/重启/日志一键操作
- **磁盘管理**：列出真实文件系统与未挂载分区，支持挂载/卸载（通过受控的 root 助手脚本）
- **登录鉴权**：访问密码 + 签名 Cookie（HttpOnly、SameSite=Lax、有效期 7 天），登录失败限流，写接口带同源校验
- **离线可用**：ECharts 已本地化，无 CDN 依赖；除 psutil 外无 pip 依赖
- **健壮性**：采集线程产出不可变快照、按类型独立渲染卡片、构建指纹检测旧页面

## 快速开始

### 1. 环境要求

- Linux（systemd 环境，测试目标为树莓派/Debian 系）
- Python 3.9+
- python3-psutil（sudo apt install python3-psutil）
- 可选：Docker（用于管理容器项目）、udisks2（用于读取机械盘 SMART 温度）

### 2. 运行

```bash
git clone https://github.com/YangZiyueZY/SysBoard.git
cd SysBoard

# 可选：设置访问密码（留空则不鉴权，仅建议在可信内网）
export SYSBOARD_PASSWORD='你的强密码'

# 可选：复制示例登记表并按本机情况修改（不复制也能用自动发现）
cp projects.json.example projects.json

python3 app.py
```

打开 <http://127.0.0.1:5000>（默认监听 0.0.0.0:5000）。

### 3. 作为 systemd 用户服务运行（可选）

创建 ~/.config/systemd/user/sysboard.service：

```ini
[Unit]
Description=SysBoard - System Monitor Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/SysBoard
Environment=SYSBOARD_PORT=5000
Environment=SYSBOARD_PASSWORD=你的强密码
ExecStart=/usr/bin/python3 %h/SysBoard/app.py
Restart=on-failure

[Install]
WantedBy=default.target
```

然后执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now sysboard
```

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| SYSBOARD_HOST | 0.0.0.0 | 监听地址 |
| SYSBOARD_PORT | 5000 | 监听端口 |
| SYSBOARD_PASSWORD | 空 | 访问密码；为空时不鉴权 |
| SYSBOARD_TOKEN | 空 | 旧版密码别名（不推荐） |
| SYSBOARD_SECRET_FILE | <项目目录>/.session_secret | 会话签名密钥文件（0600，自动生成） |
| SYSBOARD_PROJECTS_FILE | <项目目录>/projects.json | 项目登记表路径 |
| SYSBOARD_INTERVAL | 2.0 | 采集周期（秒） |
| SYSBOARD_DISABLE_CONTROL | 空 | 设为 1 时面板只读，拒绝所有启停请求 |
| SYSBOARD_CTL_HELPER | /usr/local/sbin/sysboard-ctl | 系统级服务控制助手路径 |

## 登录与鉴权

- 未登录访问 / 会跳转到 /login；登录页自包含，不依赖静态资源
- Cookie 值为时间戳加 HMAC-SHA256 签名，签名密钥首次登录时随机生成并落盘
- 修改访问密码会让旧 Cookie 自动失效；服务重启不会踢掉已登录浏览器
- 同一来源 10 分钟内错 8 次密码即 429 限流
- 脚本/自动化可用请求头访问：curl -H 'X-SysBoard-Token: 你的密码' http://127.0.0.1:5000/api/stats
- ?token= 查询串方式已移除，避免密码进入 URL、历史记录与 Referer
- 写接口（/api/ctl、/api/disk、/api/projects、/logout）除登录态外还要求同源校验
- 若经隧道或反向代理对外暴露，请在隧道侧强制 HTTPS（Cookie 未设置 Secure 标志）

## 项目登记表 projects.json

页面底部「项目」卡片的数据来自两部分：登记表（可定制）与自动发现（systemd 单元 + Docker 容器）。两者按 name 去重，登记表写法优先。

仓库提供 projects.json.example 作为格式参考，复制为 projects.json 后即可编辑；该文件已加入 .gitignore，不会被提交。

字段简述：

| 字段 | 说明 |
|---|---|
| name | 必填，唯一 id；systemd 项目同时是单元名 |
| title / desc | 卡片显示名与说明 |
| kind | systemd / docker / port |
| scope | systemd 项目：user 或 system |
| unit / container | 指定单元名或容器名（缺省用 name） |
| port / path / link | Web 端口、路径、是否生成直达链接 |
| group / order | 分组与组内排序 |
| hidden | 是否隐藏 |

页面内每张卡片右上角 ✎ 可直接编辑；保存会原子写回 projects.json 并保留 .bak 备份。分组支持新建、改名与删除。登记表里写了但本机已不存在的条目会折叠到页脚提示，不再占列表位置。

## 系统级服务控制（免密助手）

要启停系统级 systemd 单元，需要 root 权限。项目提供一个经过白名单校验的助手脚本 tools/sysboard-ctl，sudoers 只放行这一个脚本：

```bash
sudo install -m 0755 ./tools/sysboard-ctl /usr/local/sbin/sysboard-ctl
printf '%s\n' "$(id -un) ALL=(root) NOPASSWD: /usr/local/sbin/sysboard-ctl" \
  | sudo tee /etc/sudoers.d/sysboard-ctl
sudo chmod 0440 /etc/sudoers.d/sysboard-ctl
```

安装后面板每分钟探测一次（也可 sudo -n /usr/local/sbin/sysboard-ctl probe 手动验证），系统级项目的按钮自动解禁。助手内部会拒绝 ssh.service、user@*.service、dbus.service、systemd-* 等危险单元。

如需完全只读，可在服务环境里设置 SYSBOARD_DISABLE_CONTROL=1，所有启停请求返回 403。

## 目录结构

| 文件 | 作用 |
|---|---|
| app.py | 后端：指标采集、项目管理、HTTP 服务与鉴权 |
| static/index.html | 页面结构（含 __BUILD__ 占位符，由后端注入指纹） |
| static/style.css | Claude 主题样式 |
| static/app.js | 前端逻辑：轮询、图表、进程表、项目卡片 |
| static/vendor/echarts.min.js | 本地化 ECharts，断网可用 |
| static/favicon.* | 网站图标 |
| projects.json.example | 项目登记表格式示例（真实 projects.json 不入库） |
| tools/sysboard-ctl | 系统级单元控制助手（需手动安装） |
| tools/make_favicon.py | 纯标准库生成图标（无 PIL/ImageMagick 也可重做） |

## 接口

| 接口 | 说明 |
|---|---|
| GET /api/stats | 全部指标快照 + 项目列表 + 构建指纹，页面每 2 秒轮询 |
| POST /api/ctl | {"name":"example-web","action":"start|stop|restart","confirm":false} |
| GET /api/logs | ?name=example-web&tail=300，返回 {source, text} |
| POST /api/projects | 页面内编辑登记表：{"action":"save|delete|group_add|group_rename|group_delete", ...} |
| POST /api/disk | {"action":"mount|unmount","device":"sda1"} |

项目名只接受登记表或自动发现中已有的名字，后端将其转成固定参数的 systemctl/docker 调用，不经过 shell。同一项目 1.5 秒内只接受一次操作，全局并发限 2 个。

## 指标说明

- **累计流量**：来自 psutil.net_io_counters()（/proc/net/dev），自开机累计，整机重启才归零；合计只统计物理出口网卡，排除 docker0/br-*/veth* 等桥接流量以免重复
- **每盘读写**：psutil.disk_io_counters(perdisk=True)，按整盘统计，过滤分区、loop、ram、zram、dm-
- **忙碌占比**：基于内核 busy_time（io_ticks）的差值，口径与 iostat %util 一致
- **机械盘温度**：读取 udisks2 的 SMART 缓存（SmartTemperature），无需 root；udisksd 约每 10 分钟重采一次，页面会标注采样延迟
- **历史曲线**：只在浏览器内存中保留最近 60 个点（约 2 分钟），刷新页面重置，不写盘

## 常见问题

- **打开地址跳到登录页**：说明设置了 SYSBOARD_PASSWORD，输入密码即可
- **一直提示密码不对**：确认环境变量名是 SYSBOARD_PASSWORD，改完需重启服务；错太多次会进入 10 分钟限流
- **页脚显示「页面已过期，请强制刷新」**：浏览器标签页加载的是旧版前端，按 Ctrl+Shift+R 强制刷新
- **系统级项目一直是只读**：免密助手未安装或未生效；检查 sudo -n /usr/local/sbin/sysboard-ctl probe 是否输出 ok
- **累计流量或硬盘温度显示「–」**：确认已安装 udisks2；温度来自 SMART 缓存，最多可能有十分钟延迟
- **没有 Docker 也能用吗**：可以。容器项目会显示为不可操作，其余功能照常

## 实现要点（节选）

- 采集固定在单个常驻线程完成，HTTP 线程只读快照，避免 psutil 按线程缓存导致 cpu_percent 恒为 0
- 进程表一次遍历同时产出 CPU/内存两份榜单，降低采集开销
- 项目资源占用通过读取 cgroup 目录统计整棵进程树，而不是逐进程打开 /proc/<pid>/cgroup
- 启停后主动作废状态缓存并唤醒采集线程，页面 1–2 秒即可看到新状态
- POST 接口先读干净请求体再做鉴权判断，避免 keep-alive 连接错位解析
- 写接口同时校验登录态、Sec-Fetch-Site、Origin 与 Content-Type，抵御跨站 POST
- 静态资源按需 gzip，但仅缓存自包含的大文件（ECharts），避免小文件构建指纹过期
- 前端按项目名复用卡片 DOM，每轮只更新文本与按钮状态，不整块重绘

## 调试与验证

```bash
# 另起一个端口测试（不影响已在 5000 端口运行的服务）
SYSBOARD_PORT=5099 SYSBOARD_HOST=127.0.0.1 python3 app.py

# 无头浏览器截图（需本机有 chromium）
chromium --headless=new --disable-gpu --no-sandbox --window-size=1400,1100 \
  --virtual-time-budget=7000 --screenshot=/tmp/shot.png http://127.0.0.1:5000/
```

页面提供若干只读调试入口：?card=projects、?card=disk-card、?card=net-card、?card=temp-card、?groupmodal=1、?diskmodal=1 等。

## 隐私与安全

- 仓库不包含任何访问密码、会话密钥、运行日志或真实项目清单
- 首次登录自动生成的 .session_secret 已加入 .gitignore；删除它会让所有已登录浏览器掉线
- 真实 projects.json 及其备份 .bak 已加入 .gitignore，示例文件仅用于说明格式
- 日志中的登录来源 IP、项目拓扑与硬件信息属于本机运行数据，不会进入版本库
- 本面板面向可信内网设计；如需公网访问，请置于强制 HTTPS 的隧道/反向代理之后，并启用访问密码

## License

Apache-2.0
