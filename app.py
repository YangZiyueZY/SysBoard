#!/usr/bin/env python3
"""SysBoard 后端：采集系统指标 + 项目管理（systemd / docker），通过 HTTP 提供 JSON 接口和静态文件。"""

import gzip
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import psutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

HOST = os.environ.get("SYSBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("SYSBOARD_PORT", "5000"))
# 登录密码。留空 = 不鉴权（只建议在内网可信环境下这么干）。
# SYSBOARD_TOKEN 作为旧配置的别名继续读，但不再支持把密码写在 URL 查询串里。
PASSWORD = os.environ.get("SYSBOARD_PASSWORD", "") or os.environ.get("SYSBOARD_TOKEN", "")
SESSION_SECRET_FILE = os.environ.get(
    "SYSBOARD_SECRET_FILE", os.path.join(BASE_DIR, ".session_secret"))
SESSION_COOKIE = "sysboard_session"
SESSION_TTL = 7 * 24 * 3600          # 一次登录管一周
LOGIN_MAX_FAILS = 8                  # 同一来源 10 分钟内最多错这么多次
LOGIN_WINDOW = 600.0
COLLECT_INTERVAL = float(os.environ.get("SYSBOARD_INTERVAL", "2.0"))
# 紧急开关：置 1 时只读，启停接口全部拒绝（Environment=SYSBOARD_DISABLE_CONTROL=1）
CONTROL_ENABLED = os.environ.get("SYSBOARD_DISABLE_CONTROL", "").lower() not in ("1", "true", "yes")

TOP_PROCESSES = 10

# 项目登记表：显式登记想管理/想定制的项目，文件改完即时生效
PROJECTS_FILE = os.environ.get("SYSBOARD_PROJECTS_FILE", os.path.join(BASE_DIR, "projects.json"))
# 系统级单元的免密控制助手（见 tools/sysboard-ctl 与 README）
CTL_HELPER = os.environ.get("SYSBOARD_CTL_HELPER", "/usr/local/sbin/sysboard-ctl")
# 磁盘挂载助手（挂载/卸载都经它，脚本内部再做一遍校验）
DISK_HELPER = "/usr/local/sbin/sysboard-disk"

USER_UNIT_DIR = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user")
SYSTEM_UNIT_DIR = "/etc/systemd/system"
# 这些单元不该出现在面板里（桌面/蓝牙运行时单元、一次性定时任务）
HIDDEN_UNITS = {"mpris-proxy", "wireplumber", "pipewire", "dbus", "*-update"}
# 停掉就等于把自己关在门外的项目：页面要求输入名字二次确认
CRITICAL_NAMES = {"sysboard"}
# systemd 的 SubState 里，属于"启动流程还没走完"的那些阶段，翻成人话
SUB_STATE_TEXT = {
    "start-pre": "前置检查（ExecStartPre）",
    "mount-pre": "挂载准备",
    "mount": "等待挂载",
    "start": "启动中",
    "start-chown": "改属主",
    "start-post": "后置检查（ExecStartPost）",
    "reload": "重载中",
}

# systemctl/docker 每次都要启进程，状态表缓存十秒足够（服务起停不是秒级事件）
CACHE_SEC = 10.0
# 免密助手是否已安装：一分钟探一次足够
HELPER_CACHE_SEC = 60.0
# 登记表按 mtime 缓存，避免每个 2 秒周期都读盘解析
PROJECTS_CACHE_SEC = 2.0
# 只有这么大的静态资源才值得把 gzip 结果缓存起来（echarts.min.js）；小文件实时压缩
GZIP_CACHE_MIN = 64 * 1024
# 磁盘温度走 udisks2 的 SMART 缓存：30 秒读一次足够（温度是慢变量，
# 每 2 秒起一次 busctl 只是白白 fork 进程）
DISK_TEMP_CACHE_SEC = 30.0
# 盘符 → udisks2 对象路径、型号、序列号这类几乎不变的静态信息，十分钟一次
DRIVE_META_CACHE_SEC = 600.0

# 这些挂载点不允许从面板卸载（系统关键路径；数据盘不在其中，因为 USB 盘拔出前应先卸载）
PROTECTED_MOUNTS = ("/", "/boot", "/boot/firmware", "/usr", "/var", "/etc", "/home")
DEVICE_RE = re.compile(r"^[a-z0-9@._+-]{1,32}$")
# 项目名/单元名的白名单：只允许 systemd 合法字符，杜绝 shell 元字符与路径穿越
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]{0,63}$")

THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"

# 免密控制助手的安装命令，面板「启用系统级服务控制」弹窗里原样展示
HELPER_INSTALL = [
    "sudo install -m 0755 %s /usr/local/sbin/sysboard-ctl"
    % os.path.join(BASE_DIR, "tools", "sysboard-ctl"),
    "printf '%s\\n' \"$(id -un) ALL=(root) NOPASSWD: /usr/local/sbin/sysboard-ctl\" "
    "| sudo tee /etc/sudoers.d/sysboard-ctl",
    "sudo chmod 0440 /etc/sudoers.d/sysboard-ctl",
]

_prev = {"disk": None, "disk_per": None, "net": None, "time": None}
_snapshot = {"stats": None}
# 采集线程的即时唤醒：启停操作后立刻重采一次，页面不用等满一个周期
_wake = threading.Event()
_svc_cache = {"time": 0.0, "user": None, "system": None}
_lsblk_cache = {"time": 0.0, "labels": None, "available": None}
_docker_cache = {"time": 0.0, "info": None, "available": False}
_ports_cache = {"time": 0.0, "owners": None}
_helper_cache = {"time": 0.0, "ready": False}
_projects_cache = {"key": None, "doc": None, "entries": [], "groups": []}
_ctl_recent = {}
_ctl_sem = threading.Semaphore(2)
_gzip_cache = {}
_login_fails = {}
_secret_cache = {"key": None}
_drive_meta_cache = {"time": 0.0, "by_name": {}}
_smart_cache = {"time": 0.0, "by_dev": {}}


# ---------------------------------------------------------------- 登录会话

def _session_secret():
    """会话签名密钥：首次使用时随机生成并落盘（0600）。

    不把密码本身写进 Cookie，也不在服务重启后把所有人踢下线 —— 签名密钥存在文件里，
    密码对了才有 Cookie，改密码后旧 Cookie 自然失效（签名对不上）。
    """
    if _secret_cache["key"]:
        return _secret_cache["key"]
    secret = b""
    try:
        with open(SESSION_SECRET_FILE, "rb") as f:
            secret = f.read().strip()
    except OSError:
        secret = b""
    if len(secret) < 32:
        secret = os.urandom(32).hex().encode()
        try:
            fd = os.open(SESSION_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secret)
        except OSError as exc:
            print("会话密钥无法落盘（重启后需重新登录）: %s" % exc, flush=True)
    _secret_cache["key"] = secret
    return secret


def _session_value():
    """Cookie 值：<过期时间戳>.<hmac>，自包含、无需服务端会话表。"""
    expires = str(int(time.time()) + SESSION_TTL)
    mac = hmac.new(_session_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return expires + "." + mac


def _session_ok(value):
    if not value or "." not in value:
        return False
    expires, _, mac = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expect = hmac.new(_session_secret(), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expect)


def _cookie_value(header, name):
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def _login_blocked(ip):
    """登录失败次数限流：内网也别让人拿字典慢慢试。"""
    now = time.time()
    hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    _login_fails[ip] = hits
    return len(hits) >= LOGIN_MAX_FAILS


def _login_record(ip, ok):
    now = time.time()
    if ok:
        _login_fails.pop(ip, None)
        return
    hits = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_WINDOW]
    hits.append(now)
    _login_fails[ip] = hits


def _run(argv, timeout=5, env=None):
    """跑一条外部命令，任何 OSError/超时都收敛成 (rc, 输出)，调用方不必各自兜异常。"""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "%s: %s" % (type(exc).__name__, exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _user_env():
    """systemctl --user / systemd-run 需要会话总线；从服务环境继承不到时补上默认值。"""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % os.getuid())
    return env


def _prime():
    """cpu_percent 依赖上一次采样的差值，启动时先采一次建立基线。"""
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)
    for proc in psutil.process_iter(["cpu_percent"]):
        pass


def _rates():
    now = time.monotonic()
    disk = psutil.disk_io_counters()
    disk_per = psutil.disk_io_counters(perdisk=True)
    net = psutil.net_io_counters(pernic=True)
    disk_rates = {"read_bps": 0.0, "write_bps": 0.0}
    net_rates = {}
    dt = None if _prev["time"] is None else now - _prev["time"]
    if dt and dt > 0:
        if disk and _prev["disk"]:
            disk_rates["read_bps"] = max(0.0, (disk.read_bytes - _prev["disk"].read_bytes) / dt)
            disk_rates["write_bps"] = max(0.0, (disk.write_bytes - _prev["disk"].write_bytes) / dt)
        if _prev["net"] is not None:
            # net_if_stats 只取一次：以前放在网卡循环里，每张网卡都要多走一趟系统调用
            ifup = psutil.net_if_stats()
            for name, counters in net.items():
                if name == "lo" or name not in _prev["net"]:
                    continue
                stats = ifup.get(name)
                if stats is not None and not stats.isup:
                    continue
                before = _prev["net"][name]
                net_rates[name] = {
                    "up_bps": max(0.0, (counters.bytes_sent - before.bytes_sent) / dt),
                    "down_bps": max(0.0, (counters.bytes_recv - before.bytes_recv) / dt),
                }
    net_totals = _net_totals(net)
    drives = _drive_rows(disk_per, _prev["disk_per"], dt)
    _prev.update(disk=disk, disk_per=disk_per, net=net, time=now)
    return disk_rates, net_rates, net_totals, drives


def _cpu():
    freq = psutil.cpu_freq()
    return {
        "percent": psutil.cpu_percent(interval=None),
        "per_cpu": psutil.cpu_percent(interval=None, percpu=True),
        "freq_mhz": round(freq.current) if freq else None,
        "count": psutil.cpu_count(),
    }


def _lsblk_info():
    """分区文件系统与卷标（未挂载的分区也能读到，走 udev 数据库），10 秒缓存。"""
    now = time.monotonic()
    if _lsblk_cache["labels"] is not None and now - _lsblk_cache["time"] < CACHE_SEC:
        return _lsblk_cache["labels"], _lsblk_cache["available"]
    labels, available = {}, []
    try:
        out = subprocess.run(
            ["lsblk", "-b", "-J", "-o", "NAME,FSTYPE,LABEL,PARTLABEL,SIZE,MOUNTPOINT,TYPE"],
            capture_output=True, text=True, timeout=5).stdout
        nodes = json.loads(out).get("blockdevices") or []

        def walk(items):
            for n in items:
                if n.get("type") == "part" and n.get("fstype") and n["fstype"] != "swap":
                    dev = "/dev/" + n["name"]
                    labels[dev] = n.get("label") or n.get("partlabel") or ""
                    if not n.get("mountpoint"):
                        available.append({
                            "dev": dev,
                            "fstype": n["fstype"],
                            "label": labels[dev],
                            "size": int(n.get("size") or 0),
                        })
                walk(n.get("children") or [])

        walk(nodes)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _lsblk_cache.update(time=now, labels=labels, available=available)
    return labels, available


def _mounts():
    """所有真实文件系统的挂载信息（含根分区），供磁盘卡片逐行列出。"""
    labels, _ = _lsblk_info()
    rows = []
    for p in psutil.disk_partitions(all=False):
        if not p.device.startswith("/dev/") or p.device.startswith("/dev/loop"):
            continue
        try:
            du = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        rows.append({
            "dev": p.device,
            "mount": p.mountpoint,
            "fstype": p.fstype,
            "label": labels.get(p.device, ""),
            "total": du.total,
            "used": du.used,
            "free": du.free,
            "percent": du.percent,
            "removable": p.mountpoint not in PROTECTED_MOUNTS,
        })
    return rows


def _temp():
    try:
        with open(THERMAL_PATH) as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for entries in (sensors or {}).values():
        if entries:
            return round(entries[0].current, 1)
    return None


# ------------------------------------------------------ 网卡累计 / 物理盘明细

# 判断「物理出口网卡」用前缀白名单：docker0、br-xxx、veth 的流量都是从
# eth0 转进去的，求合计时必须排除，否则容器流量被算两遍
_EXTERNAL_NIC_PREFIX = ("eth", "en", "wl", "wlan", "usb", "ppp")


def _read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _net_totals(net):
    """自开机累计流量：直接取内核计数器（这些数字重启才归零）。

    与速率共用同一次 psutil.net_io_counters(pernic=True) 的结果，
    不为累计值多走一趟系统调用。
    """
    totals = {}
    for name, counters in net.items():
        if name == "lo":
            continue
        totals[name] = {
            "down_bytes": counters.bytes_recv,
            "up_bytes": counters.bytes_sent,
            "down_packets": counters.packets_recv,
            "up_packets": counters.packets_sent,
        }
    return totals


def _net_total_sum(totals):
    """全网卡合计（只加物理出口网卡，口径见 _EXTERNAL_NIC_PREFIX）。"""
    down = up = 0
    for name, row in totals.items():
        if not name.startswith(_EXTERNAL_NIC_PREFIX):
            continue
        down += row["down_bytes"]
        up += row["up_bytes"]
    return {"down_bytes": down, "up_bytes": up}


def _udisks_get(path, iface, prop):
    """读 udisks2 的一个属性。输出形如 `d 313.15` / `b false` / `s "..." `，
    失败（服务不在、盘不支持）返回 None，调用方自行降级。"""
    rc, out = _run(["busctl", "get-property", "org.freedesktop.UDisks2", path,
                    iface, prop], timeout=3)
    if rc != 0:
        return None
    parts = out.split(None, 1)
    if len(parts) < 2:
        return None
    value = parts[1].strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value


def _drive_meta(per):
    """每块物理盘的静态信息 + udisks2 对象路径（十分钟缓存）。

    从 psutil 的 per-disk 计数里挑整块设备：/sys/block/<name>/queue/rotational
    存在的才是（分区、loop、ram 都没有这个文件），再排掉回环/内存盘/光驱。
    型号与序列号优先问 udisks：USB 桥接盒的 SCSI model 往往是桥片自己的名字
    （USB 桥接盒的 sysfs model 可能只是桥片厂商名），ATA 盘真正的型号在
    udisks 的 Drive.Model 里。
    """
    now = time.monotonic()
    cached = _drive_meta_cache["by_name"]
    if cached and now - _drive_meta_cache["time"] < DRIVE_META_CACHE_SEC:
        return cached
    rows = {}
    for name in per:
        if name.startswith(("loop", "ram", "zram", "dm-", "sr", "fd")):
            continue
        sysdir = "/sys/block/%s" % name
        if _read_text(os.path.join(sysdir, "queue/rotational")) == "":
            continue
        try:
            size = int(_read_text(os.path.join(sysdir, "size")) or 0) * 512
        except ValueError:
            size = 0
        row = {
            "name": name,
            "rotational": _read_text(os.path.join(sysdir, "queue/rotational")) == "1",
            "size": size,
            "model": _read_text(os.path.join(sysdir, "device/model")),
            "serial": _read_text(os.path.join(sysdir, "device/serial")),
            "udisks": "",
        }
        path = _udisks_get("/org/freedesktop/UDisks2/block_devices/%s" % name,
                           "org.freedesktop.UDisks2.Block", "Drive")
        if path and path != "/":
            row["udisks"] = path
            row["model"] = _udisks_get(path, "org.freedesktop.UDisks2.Drive", "Model") or row["model"]
            row["serial"] = _udisks_get(path, "org.freedesktop.UDisks2.Drive", "Serial") or row["serial"]
        rows[name] = row
    _drive_meta_cache.update(time=now, by_name=rows)
    return rows


def _smart_temps(meta):
    """机械盘温度：读 udisks2 的 SMART 缓存，全程不需要 root。

    SmartUpdate 方法要 polkit 授权（非会话进程会吃 Not authorized），只能读
    udisksd 的缓存。缓存由 udisksd 自己每 10 分钟重采一次 SMART（实测
    SmartUpdated 每 600 秒前进一次），所以温度最旧可能差十分钟——这期间的
    值仍然是稳定的（温度是慢变量）。TTL 30 秒只为少起几个 busctl 进程。
    SmartUpdated 一起下发，页面据此标注采样延迟，不会被误当成实时温度。
    """
    now = time.monotonic()
    if _smart_cache["by_dev"] and now - _smart_cache["time"] < DISK_TEMP_CACHE_SEC:
        return _smart_cache["by_dev"]
    temps = {}
    for name, row in meta.items():
        path = row.get("udisks")
        if not path:
            continue
        kelvin = _udisks_get(path, "org.freedesktop.UDisks2.Drive.Ata", "SmartTemperature")
        try:
            celsius = round(float(kelvin) - 273.15, 1)
        except (TypeError, ValueError):
            continue          # SD 卡 / U 盘这类没有 Ata 接口的盘，照常显示但无温度
        updated = _udisks_get(path, "org.freedesktop.UDisks2.Drive.Ata", "SmartUpdated")
        failing = _udisks_get(path, "org.freedesktop.UDisks2.Drive.Ata", "SmartFailing")
        try:
            updated = int(updated or 0)
        except ValueError:
            updated = 0
        temps[name] = {"temp_c": celsius, "temp_updated": updated,
                       "temp_failing": failing == "true"}
    _smart_cache.update(time=now, by_dev=temps)
    return temps


def _drive_rows(per, prev, dt):
    """每块物理盘一行：自开机累计读写、当前速率、忙碌占比，机械盘带温度。

    忙碌占比用 busy_time（内核的 io_ticks，毫秒）差值算，比
    (read_time + write_time) 准——后者把并发 IO 的时间重复计入。
    """
    meta = _drive_meta(per)
    temps = _smart_temps(meta)
    rows = []
    # 机械盘排前面，SD 卡这类靠后
    for name, row in sorted(meta.items(), key=lambda kv: (not kv[1]["rotational"], kv[0])):
        io = per.get(name)
        if io is None:
            continue
        rate_read = rate_write = busy = 0.0
        if prev and dt:
            before = prev.get(name)
            if before is not None:
                rate_read = max(0.0, (io.read_bytes - before.read_bytes) / dt)
                rate_write = max(0.0, (io.write_bytes - before.write_bytes) / dt)
                busy = min(100.0, max(0.0, (io.busy_time - before.busy_time) / dt / 10.0))
        entry = {
            "name": name,
            "dev": "/dev/" + name,
            "model": row["model"],
            "serial": row["serial"],
            "rotational": row["rotational"],
            "size": row["size"],
            "read_bytes": io.read_bytes,
            "write_bytes": io.write_bytes,
            "read_count": io.read_count,
            "write_count": io.write_count,
            "read_bps": round(rate_read, 1),
            "write_bps": round(rate_write, 1),
            "busy": round(busy, 1),
        }
        if name in temps:
            entry.update(temps[name])
        rows.append(entry)
    return rows


def _proc_table():
    """一趟遍历拿到全部进程（CPU/内存/rss），进程表与项目资源都从这里出。

    以前是 process_iter 走两遍（按 CPU 一遍、按内存一遍），每次都重建整张进程表，
    在 Pi 上这是采集里最贵的一段；现在一趟出两份榜单，项目占用再按 cgroup 归属求和。
    """
    total_mem = psutil.virtual_memory().total or 1
    rows = []
    for proc in psutil.process_iter(["name", "username", "cpu_percent", "memory_info", "create_time"]):
        info = proc.info
        try:
            mem_info = info.get("memory_info")
            rss = int(mem_info.rss) if mem_info else 0
            rows.append({
                "pid": proc.pid,
                "name": info.get("name") or "?",
                "user": info.get("username") or "?",
                "cpu": round(info.get("cpu_percent") or 0.0, 1),
                "mem": round(rss * 100.0 / total_mem, 1),
                "rss": rss,
                "created": info.get("create_time") or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


# ---------------------------------------------------------------- cgroup 归属

CG_ROOT = "/sys/fs/cgroup"


def _unit_cg_dirs(scope, unit):
    """单元可能的 cgroup 目录。用户级单元默认在 app.slice 下，被会话/切片规则改过也一并试。"""
    uid = os.getuid()
    if scope == "user":
        base = "%s/user.slice/user-%d.slice/user@%d.service" % (CG_ROOT, uid, uid)
        return [os.path.join(base, "app.slice", unit),
                os.path.join(base, unit),
                os.path.join(base, "session.slice", unit),
                os.path.join(base, "service.slice", unit)]
    return ["%s/system.slice/%s" % (CG_ROOT, unit), "%s/%s" % (CG_ROOT, unit)]


def _cg_pids(dirs):
    """这些 cgroup 目录（含子 cgroup）里的所有 pid。

    读 cgroup.procs 而不是逐个进程读 /proc/<pid>/cgroup：项目只有十几个，
    每个一次目录遍历，比给 200 个进程各开一次文件便宜一个量级。
    """
    pids = set()
    for path in dirs:
        if not os.path.isdir(path):
            continue
        for root, _subdirs, files in os.walk(path):
            if "cgroup.procs" not in files:
                continue
            try:
                with open(os.path.join(root, "cgroup.procs")) as handle:
                    for line in handle:
                        try:
                            pids.add(int(line))
                        except ValueError:
                            continue
            except OSError:
                continue
    return pids


def _top(procs, key, limit):
    rows = sorted(procs, key=lambda item: item[key], reverse=True)[:limit]
    return [{k: row[k] for k in ("pid", "name", "user", "cpu", "mem", "rss")} for row in rows]


# ---------------------------------------------------------------- 项目登记表

def _norm_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def _norm_text(value, limit):
    return str(value if value is not None else "").strip()[:limit]


def _load_registry():
    """读 projects.json（按 mtime 缓存）。

    返回 {"doc": 原始 JSON 对象, "entries": 规范化条目, "groups": [分组名]}。
    保留 doc 是为了「页面里改一下卡片」时只动该动的那几个字段：用户手写的
    _说明/_格式、以及本程序不认识的字段，原样写回去，不整份重排。
    """
    try:
        stat = os.stat(PROJECTS_FILE)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return _projects_cache if _projects_cache["doc"] is not None else {
            "doc": {"projects": []}, "entries": [], "groups": []}
    if _projects_cache["key"] == key:
        return _projects_cache
    try:
        with open(PROJECTS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        print("projects.json 解析失败，沿用上一份: %s" % exc, flush=True)
        if _projects_cache["doc"] is not None:
            return _projects_cache
        return {"doc": {"projects": []}, "entries": [], "groups": []}

    if not isinstance(raw, dict):
        raw = {"projects": raw if isinstance(raw, list) else []}
    items = raw.get("projects")
    if not isinstance(items, list):
        items = []
        raw["projects"] = items

    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        kind = str(item.get("kind") or "systemd").strip().lower()
        if not NAME_RE.match(name) or kind not in ("systemd", "docker", "port"):
            print("projects.json 跳过非法条目: %r" % item, flush=True)
            continue
        scope = str(item.get("scope") or ("user" if kind == "systemd" else "")).strip().lower()
        if kind == "systemd" and scope not in ("user", "system"):
            print("projects.json 跳过 scope 非法的条目: %r" % item, flush=True)
            continue
        unit = str(item.get("unit") or (name + ".service" if kind == "systemd" else "")).strip()
        if kind == "systemd" and not NAME_RE.match(unit):
            print("projects.json 跳过 unit 非法的条目: %r" % item, flush=True)
            continue
        entries.append({
            "name": name,
            "title": str(item.get("title") or name),
            "desc": str(item.get("desc") or ""),
            "kind": kind,
            "scope": scope,
            "unit": unit,
            "container": str(item.get("container") or name),
            "port": _norm_port(item.get("port")),
            "path": str(item.get("path") or ""),
            "link": bool(item.get("link", True)),
            "group": str(item.get("group") or "未分组"),
            "order": int(item.get("order") or 100),
            "critical": bool(item.get("critical")) or name in CRITICAL_NAMES,
            "hidden": bool(item.get("hidden")),
        })

    groups = [g for g in (raw.get("groups") or []) if isinstance(g, str) and g.strip()]
    _projects_cache.update(key=key, doc=raw, entries=entries, groups=groups)
    return _projects_cache


def _write_registry(doc):
    """原子写回登记表，并留一份上一版备份。"""
    mode = 0o600
    try:
        mode = os.stat(PROJECTS_FILE).st_mode & 0o777
    except OSError:
        pass
    tmp = PROJECTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode)
    try:
        shutil.copy2(PROJECTS_FILE, PROJECTS_FILE + ".bak")
    except OSError:
        pass
    os.replace(tmp, PROJECTS_FILE)
    _projects_cache.update(key=None, doc=None, entries=[], groups=[])
    _wake.set()          # 让采集线程立刻重出快照，页面上保存即可见
    print("[projects] 登记表已更新", flush=True)


def _group_rank(entries, groups):
    """分组显示顺序：显式 groups 数组优先，其余按登记表出现顺序，「自动发现」永远垫底。"""
    rank = {}
    for name in groups:
        rank.setdefault(name, len(rank))
    for entry in entries:
        rank.setdefault(entry["group"], len(rank))
    rank.setdefault("自动发现", 10 ** 6)
    return rank


def _edit_projects(body):
    """页面内的卡片编辑：改显示名/说明/分组/端口/路径，以及分组的增删改名。

    返回 (http 状态码, 结果字典)。只改登记表，不碰单元文件。
    """
    action = str(body.get("action") or "").strip().lower()
    reg = _load_registry()
    doc = json.loads(json.dumps(reg["doc"]))          # 深拷贝，失败时不污染缓存
    items = doc["projects"]
    groups = [g for g in (doc.get("groups") or []) if isinstance(g, str)]
    # 项目正在使用的分组也要算进来：新建/改名时的重名检查只看显式数组就会漏
    # （实测过：把分组改名成「机器人」时没报错，直接把几个项目并了进去）
    for item in items:
        if isinstance(item, dict):
            used = str(item.get("group") or "未分组")
            if used not in groups:
                groups.append(used)

    def find(name):
        for item in items:
            if isinstance(item, dict) and item.get("name") == name:
                return item
        return None

    def touch_groups():
        if groups:
            doc["groups"] = groups
        else:
            doc.pop("groups", None)

    if action == "group_add":
        name = _norm_text(body.get("group"), 40)
        if not name:
            return 400, {"ok": False, "message": "分组名不能为空"}
        if name in groups:
            return 409, {"ok": False, "message": "分组「%s」已经存在" % name}
        groups.append(name)
        touch_groups()
        _write_registry(doc)
        return 200, {"ok": True, "message": "已新建分组「%s」" % name}

    if action == "group_rename":
        src = _norm_text(body.get("group"), 40)
        dst = _norm_text(body.get("to"), 40)
        if not src or not dst:
            return 400, {"ok": False, "message": "分组名不能为空"}
        if dst in groups and dst != src:
            return 409, {"ok": False, "message": "分组「%s」已经存在" % dst}
        hit = 0
        for item in items:
            if isinstance(item, dict) and item.get("group") == src:
                item["group"] = dst
                hit += 1
        groups = [dst if g == src else g for g in groups]
        if dst not in groups:
            groups.append(dst)
        touch_groups()
        _write_registry(doc)
        return 200, {"ok": True, "message": "「%s」已改名为「%s」，%d 个项目跟着走" % (src, dst, hit)}

    if action == "group_delete":
        name = _norm_text(body.get("group"), 40)
        used = [i.get("name") for i in items
                if isinstance(i, dict) and i.get("group") == name]
        if used:
            return 409, {"ok": False,
                         "message": "分组「%s」里还有 %d 个项目（%s），先移走再删"
                                    % (name, len(used), "、".join(str(u) for u in used[:4]))}
        groups = [g for g in groups if g != name]
        touch_groups()
        _write_registry(doc)
        return 200, {"ok": True, "message": "已删除空分组「%s」" % name}

    if action == "delete":
        name = _norm_text(body.get("name"), 64)
        item = find(name)
        if item is None:
            return 404, {"ok": False, "message": "登记表里没有「%s」这条" % name}
        items.remove(item)
        _write_registry(doc)
        return 200, {"ok": True, "message": "已取消登记「%s」（自装服务仍会出现在自动发现里）" % name}

    if action != "save":
        return 400, {"ok": False, "message": "不支持的编辑动作：%s" % (action or "(空)")}

    # ---- save：改一张卡片的显示字段 ----
    name = _norm_text(body.get("name"), 64)
    if not NAME_RE.match(name):
        return 400, {"ok": False, "message": "项目名非法"}
    live = next((e for e in _project_index() if e["name"] == name), None)
    if live is None:
        return 404, {"ok": False, "message": "没有名为「%s」的项目" % name}

    title = _norm_text(body.get("title"), 60) or name
    desc = _norm_text(body.get("desc"), 200)
    group = _norm_text(body.get("group"), 40) or "未分组"
    path = _norm_text(body.get("path"), 60)
    if path and not path.startswith("/"):
        return 400, {"ok": False, "message": "端口路径要以 / 开头（例如 /ui）"}
    raw_port = body.get("port")
    if raw_port in (None, "", "null"):
        port = None
    else:
        port = _norm_port(raw_port)
        if port is None:
            return 400, {"ok": False, "message": "端口要填 1-65535 的数字，或者留空"}
    link = bool(body.get("link", True))

    item = find(name)
    if item is None:
        # 自动发现的项目：页面里改一次就把它登记下来（登记表优先于自动发现）
        item = {"name": name, "kind": live["kind"]}
        if live["kind"] == "systemd":
            item["scope"] = live["scope"]
            item["unit"] = live["unit"]
        elif live["kind"] == "docker":
            item["container"] = live["container"]
        items.append(item)
        created = True
    else:
        created = False

    old_group = str(item.get("group") or "未分组")
    order = int(item.get("order") or 0)
    if group != old_group or not order:
        # 换组时排到目标组末尾，避免带着旧 order 插到别人前面
        siblings = [int(i.get("order") or 0) for i in items
                    if isinstance(i, dict) and i.get("group") == group and i is not item]
        order = (max(siblings) + 10) if siblings else 10

    item.update({"title": title, "desc": desc, "group": group, "order": order})
    if port is None:
        item.pop("port", None)
    else:
        item["port"] = port
    if path:
        item["path"] = path
    else:
        item.pop("path", None)
    item["link"] = link
    if group not in groups:
        groups.append(group)
    touch_groups()
    _write_registry(doc)
    verb = "已登记" if created else "已保存"
    return 200, {"ok": True, "created": created,
                 "message": "%s「%s」%s" % (verb, title, "（分组 %s）" % group if group else "")}


def _local_units(directory):
    """目录下用户自己放的实体单元文件（软链=包管理器装的，不算），返回单元名集合。"""
    names = set()
    try:
        for fn in os.listdir(directory):
            if not fn.endswith(".service"):
                continue
            full = os.path.join(directory, fn)
            if os.path.isfile(full) and not os.path.islink(full):
                names.add(fn)
    except OSError:
        pass
    return names


def _project_index():
    """登记表 + 自动发现，按 name 去重（登记表优先），返回排好序的项目列表。"""
    reg = _load_registry()
    ranked = _group_rank(reg["entries"], reg["groups"])
    ordered, seen = [], set()
    for entry in reg["entries"]:
        if entry["hidden"] or entry["name"] in seen:
            continue
        seen.add(entry["name"])
        entry["auto"] = False
        ordered.append(entry)

    auto_group = "自动发现"
    auto = []
    # 自装的用户级单元：任何状态都收（停着的也要能在面板里启动）
    for unit in sorted(_local_units(USER_UNIT_DIR)):
        name = unit[:-8]
        if name in seen or name in HIDDEN_UNITS:
            continue
        seen.add(name)
        auto.append({"name": name, "title": name, "desc": "自动发现的自装用户级服务",
                     "kind": "systemd", "scope": "user", "unit": unit, "container": name,
                     "port": None, "path": "", "link": True, "group": auto_group,
                     "order": 100, "critical": name in CRITICAL_NAMES, "hidden": False, "auto": True})
    # 自装的系统级单元
    for unit in sorted(_local_units(SYSTEM_UNIT_DIR)):
        name = unit[:-8]
        if name in seen or name in HIDDEN_UNITS:
            continue
        seen.add(name)
        auto.append({"name": name, "title": name, "desc": "自动发现的自装系统级服务",
                     "kind": "systemd", "scope": "system", "unit": unit, "container": name,
                     "port": None, "path": "", "link": True, "group": auto_group,
                     "order": 200, "critical": name in CRITICAL_NAMES, "hidden": False, "auto": True})
    # docker 容器
    for name in sorted(_docker_info()[0]):
        if name in seen:
            continue
        seen.add(name)
        auto.append({"name": name, "title": name, "desc": "自动发现的 Docker 容器",
                     "kind": "docker", "scope": "", "unit": "", "container": name,
                     "port": None, "path": "", "link": False, "group": auto_group,
                     "order": 300, "critical": False, "hidden": False, "auto": True})

    ordered.extend(sorted(auto, key=lambda e: (e["scope"] != "user", e["order"], e["name"])))
    ordered.sort(key=lambda e: (ranked.get(e["group"], 10 ** 5), e["order"], e["name"]))
    return ordered


# ---------------------------------------------------------------- systemd / docker 状态

def _unit_states(scope):
    """单元活动状态（list-units）与单元文件启用状态（list-unit-files）。"""
    flags = ["--user"] if scope == "user" else []
    env = _user_env() if scope == "user" else None
    states, files = {}, {}
    rc, out = _run(["systemctl"] + flags + ["list-units", "--type=service", "--all",
                                           "--plain", "--no-legend", "--no-pager"], env=env)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4 or not parts[0].endswith(".service"):
                continue
            states[parts[0]] = {"active": parts[2], "sub": parts[3]}
    rc, out = _run(["systemctl"] + flags + ["list-unit-files", "--type=service",
                                            "--plain", "--no-legend", "--no-pager"], env=env)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].endswith(".service"):
                files[parts[0]] = parts[1]
    # 失败单元补一次退出码：光看 list-units 只知道 failed，分不清"启动失败"和
    # "被人停掉但没优雅退出"（退出码 130/143）。失败单元通常 0~2 个，一次 show 就够。
    failed = [unit for unit, st in states.items() if st["active"] == "failed"]
    if failed:
        rc, out = _run(["systemctl"] + flags + ["show", "-p", "Id", "-p", "ExecMainStatus"] + failed,
                       env=env)
        if rc == 0:
            for block in out.split("\n\n"):
                unit, status = None, None
                for line in block.splitlines():
                    key, _, value = line.partition("=")
                    if key == "Id":
                        unit = value
                    elif key == "ExecMainStatus":
                        try:
                            status = int(value)
                        except ValueError:
                            status = None
                if unit in states and status is not None:
                    states[unit]["exit_status"] = status
    return {"states": states, "files": files}


def _svc_tables():
    now = time.monotonic()
    if _svc_cache["user"] is not None and now - _svc_cache["time"] < CACHE_SEC:
        return _svc_cache["user"], _svc_cache["system"]
    user, system = _unit_states("user"), _unit_states("system")
    _svc_cache.update(time=now, user=user, system=system)
    return user, system


def _docker_info():
    """docker ps -a 的容器表（10 秒缓存），返回 (name -> 信息, docker 是否可用)。"""
    now = time.monotonic()
    if _docker_cache["info"] is not None and now - _docker_cache["time"] < CACHE_SEC:
        return _docker_cache["info"], _docker_cache["available"]
    info, available = {}, False
    if shutil.which("docker"):
        rc, out = _run(["docker", "ps", "-a", "--no-trunc",
                        "--format", "{{.Names}}\t{{.ID}}\t{{.State}}\t{{.Status}}\t{{.Image}}"], timeout=8)
        if rc == 0:
            available = True
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 5:
                    info[parts[0]] = {"id": parts[1][:12], "full_id": parts[1],
                                      "state": parts[2].lower(), "status": parts[3],
                                      "image": parts[4]}
    _docker_cache.update(time=now, info=info, available=available)
    return info, available


def _helper_ready():
    """免密控制助手装好了没（一分钟探一次）。没装时系统级项目只读并给出安装提示。"""
    now = time.monotonic()
    if now - _helper_cache["time"] < HELPER_CACHE_SEC:
        return _helper_cache["ready"]
    ready = False
    if os.path.isfile(CTL_HELPER):
        rc, out = _run(["sudo", "-n", CTL_HELPER, "probe"], timeout=5)
        ready = rc == 0 and out.strip().startswith("ok")
    _helper_cache.update(time=now, ready=ready)
    return ready


def _port_owners():
    """监听端口 -> (pid, 进程名)。只能看到本用户有权限读的进程，够面板提示用。"""
    now = time.monotonic()
    if _ports_cache["owners"] is not None and now - _ports_cache["time"] < 5.0:
        return _ports_cache["owners"]
    owners = {}
    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        conns = []
    for conn in conns:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        port = conn.laddr.port
        if port in owners or not conn.pid:
            continue
        name = "?"
        try:
            name = psutil.Process(conn.pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        owners[port] = (conn.pid, name)
    _ports_cache.update(time=now, owners=owners)
    return owners


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- 项目行组装

def _unit_exists(scope, unit, files, local_units):
    """单元在本机是否真的存在。

    只看 list-units/list-unit-files 会把模板实例误判成不存在（postgresql@17-main.service
    这类实例不出现在 list-unit-files 里，只有 postgresql@.service 模板），所以带 @ 的
    再查一次模板；另外刚放进目录还没 daemon-reload 的单元也算存在。
    """
    if unit in files:
        return True
    if "@" in unit and (unit.split("@", 1)[0] + "@.service") in files:
        return True
    return unit in local_units


def _project_rows(procs):
    """每个项目的状态、资源占用（按 cgroup 汇总整棵进程树）与可操作性。

    返回 (要显示的项目, 已不存在的条目)。后者是登记表里写了但本机已经没有对应单元/容器/监听的
    项目 —— 面板不再列出它们，只在页脚报一句，避免删掉的服务长期挂在列表里。
    """
    user, system = _svc_tables()
    docker, docker_ok = _docker_info()
    helper = _helper_ready()
    owners = _port_owners()
    local_units = _local_units(USER_UNIT_DIR) | _local_units(SYSTEM_UNIT_DIR)
    total_mem = psutil.virtual_memory().total or 1
    now = time.time()
    rows = []
    for entry in _project_index():
        row = {k: entry[k] for k in ("name", "title", "desc", "kind", "scope", "port",
                                     "path", "link", "group", "critical", "auto")}
        row.update({"unit": entry["unit"], "container": entry["container"],
                    "enabled": "", "sub": "", "state": "stopped", "state_text": "已停止", "up": False,
                    "manageable": False, "why": "", "hint": "", "cpu": None, "mem": None,
                    "rss": None, "pid": None, "uptime_sec": None, "note": ""})

        if entry["kind"] == "systemd":
            table = user if entry["scope"] == "user" else system
            state = table["states"].get(entry["unit"])
            row["enabled"] = table["files"].get(entry["unit"], "")
            exists = _unit_exists(entry["scope"], entry["unit"], table["files"], local_units)
            row["manageable"] = bool(exists and CONTROL_ENABLED
                                     and (entry["scope"] == "user" or helper))
            if not exists:
                row["state"] = "missing"
                row["state_text"] = "单元不存在"
                row["why"] = "本机没有 %s 这个单元，登记表里的条目可能已过期" % entry["unit"]
            elif not row["manageable"]:
                row["why"] = ("只读模式（SYSBOARD_DISABLE_CONTROL=1）" if not CONTROL_ENABLED
                              else "需要安装免密助手 sysboard-ctl 才能操作系统级单元")
            if state:
                active, sub = state["active"], state["sub"]
                row["sub"] = sub
                row["up"] = active in ("active", "activating")
                row["state"] = {"active": "running", "activating": "starting", "deactivating": "stopping",
                                "failed": "failed", "inactive": "stopped"}.get(active, "other")
                row["state_text"] = {"running": "运行中", "starting": "启动中", "stopping": "停止中",
                                     "failed": "启动失败", "stopped": "已停止"}.get(row["state"], active)
                if active == "active" and sub == "exited":
                    row["state_text"] = "运行中 · 已退出"      # Type=oneshot 的一次性任务
                # 卡在启动阶段要直说：只写「启动中」会把"每 130 秒重试一次的失败"
                # 看成"再等等就好"（本机 usb-backup-server 就这么被误看过几天）
                elif active == "activating" and sub in SUB_STATE_TEXT:
                    row["state_text"] = "卡在启动 · " + SUB_STATE_TEXT[sub]
                elif active == "failed":
                    # 130/143 = 128+SIGINT/SIGTERM：进程是被信号终止的，最常见的原因是
                    # 「刚被人停掉，但程序没优雅退出」，不是启动失败。QQAIRobot 就是 130。
                    # 面板自己发起的停止会 reset-failed 清掉失败态，这里兜住"从命令行停的"。
                    exit_status = state.get("exit_status")
                    if exit_status in (130, 143):
                        row["state"] = "stopped"
                        row["state_text"] = "已停止（进程退出码 %d）" % exit_status
                        row["up"] = False
                    elif exit_status is not None:
                        row["state_text"] = "启动失败（退出码 %d）" % exit_status
        elif entry["kind"] == "docker":
            info = docker.get(entry["container"])
            if info is None:
                row["state"] = "other"
                row["state_text"] = "未找到容器"
                row["manageable"] = False
                row["why"] = "docker 里没有名为 %s 的容器" % entry["container"]
            else:
                state = info["state"]
                row["up"] = state == "running"
                row["state"] = {"running": "running", "restarting": "starting",
                                "paused": "other"}.get(state, "stopped")
                row["state_text"] = {"running": "运行中", "restarting": "重启中",
                                     "paused": "已暂停"}.get(state, "已停止")
                row["note"] = info["status"]
                row["manageable"] = CONTROL_ENABLED and docker_ok
                row["why"] = "" if row["manageable"] else (
                    "只读模式（SYSBOARD_DISABLE_CONTROL=1）" if not CONTROL_ENABLED
                    else "docker 命令不可用")
        else:  # port：只探测端口，没有可启停的目标
            row["state"] = "running" if (entry["port"] and entry["port"] in owners) or (
                entry["port"] and _port_open(entry["port"])) else "stopped"
            row["up"] = row["state"] == "running"
            row["state_text"] = "运行中" if row["up"] else "已停止"
            row["manageable"] = False
            row["why"] = "登记为端口探测项目，没有可启停的目标"

        # 按 cgroup 汇总整个项目的 CPU / 内存（含全部子进程）
        if entry["kind"] == "docker":
            full_id = (docker.get(entry["container"]) or {}).get("full_id") or ""
            dirs = ["%s/system.slice/docker-%s.scope" % (CG_ROOT, full_id)] if full_id else []
        elif entry["kind"] == "systemd":
            dirs = _unit_cg_dirs(entry["scope"], entry["unit"])
        else:
            dirs = []
        pids = _cg_pids(dirs)
        if pids:
            cpu = rss = 0.0
            pid, created = None, None
            for proc in procs:
                if proc["pid"] not in pids:
                    continue
                cpu += proc["cpu"]
                rss += proc["rss"]
                if created is None or (proc["created"] and proc["created"] < created):
                    created, pid = proc["created"], proc["pid"]
            row["cpu"] = round(cpu, 1)
            row["rss"] = int(rss)
            row["mem"] = round(rss * 100.0 / total_mem, 1)
            row["pid"] = pid
            if created:
                row["uptime_sec"] = max(0, int(now - created))

        # 单元停着但端口被别的进程占着：说明有个没纳管的裸进程在跑（例如手工拉起的服务）
        if entry["port"] and not row["up"] and entry["port"] in owners:
            holder_pid, holder_name = owners[entry["port"]]
            row["hint"] = "端口 :%d 被未纳管进程占用（pid %d %s）" % (entry["port"], holder_pid, holder_name)

        # 判断"已经不存在"（区别于"存在但停着"）：这类条目不再出现在列表里，
        # 只在页脚报个数，免得删掉的项目长期占着位置。存在的停用项目照旧显示，方便一键启动。
        if entry["kind"] == "systemd":
            row["gone"] = not exists
        elif entry["kind"] == "docker":
            row["gone"] = docker.get(entry["container"]) is None
        else:  # port：没有监听进程就当它已经没了
            row["gone"] = not row["up"]
        rows.append(row)
    live = [row for row in rows if not row["gone"]]
    gone = [{"name": row["name"], "kind": row["kind"],
             "reason": row["why"] if row["kind"] != "port"
             else "端口 :%s 没有监听进程" % row["port"]} for row in rows if row["gone"]]
    return live, gone


# ---------------------------------------------------------------- 日志

def _unit_fragment(unit, scope):
    dirs = [USER_UNIT_DIR] if scope == "user" else [SYSTEM_UNIT_DIR, "/usr/lib/systemd/system"]
    for directory in dirs:
        path = os.path.join(directory, unit)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            except OSError:
                continue
    return ""


def _append_log_paths(fragment, unit):
    """从单元文件里找 StandardOutput/StandardError=append:<文件> 的落盘路径。"""
    paths = []
    for line in fragment.splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        match = re.match(r"^Standard(?:Output|Error)\s*=\s*append:(.+)$", line)
        if not match:
            continue
        path = match.group(1).strip()
        path = path.replace("%h", os.path.expanduser("~"))
        path = path.replace("%n", unit)
        path = path.replace("%N", unit[:-8] if unit.endswith(".service") else unit)
        if path not in paths:
            paths.append(path)
    return paths


def _tail_file(path, lines):
    """读文件末尾若干行；文件大于 64KB 时从中间截断，丢掉半行。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 65536:
                f.seek(size - 65536)
                data = f.read()
                text = data.decode("utf-8", "replace")
                return "\n".join(text.splitlines()[1:][-lines:])
            text = f.read().decode("utf-8", "replace")
    except OSError as exc:
        return None
    return "\n".join(text.splitlines()[-lines:])


def _project_logs(entry, lines):
    """返回 (是否成功, 日志来源说明, 文本)。数据源随项目类型走：容器日志 / 落盘文件 / journald。"""
    lines = max(1, min(1000, lines))
    if entry["kind"] == "docker":
        rc, out = _run(["docker", "logs", "--tail", str(lines), "--timestamps", entry["container"]], timeout=15)
        if rc != 0:
            return False, "docker logs", (out or "docker logs 执行失败").strip()
        return True, "docker logs --tail %d" % lines, out.strip() or "（容器没有输出）"
    if entry["kind"] != "systemd":
        return False, "", "该项目登记为端口探测，没有可读日志"

    unit, scope = entry["unit"], entry["scope"]
    paths = [p for p in _append_log_paths(_unit_fragment(unit, scope), unit) if os.path.isfile(p)]
    if paths:
        text = _tail_file(paths[0], lines)
        if text is not None:
            return True, paths[0], text or "（日志文件是空的）"
    # 没有落盘日志就走 journald：用户级单元的日志记在系统 journal 里，
    # 用 _SYSTEMD_USER_UNIT= 过滤（本机用户级 journald 没在跑，journalctl --user 读不到）
    if scope == "user":
        argv = ["journalctl", "_SYSTEMD_USER_UNIT=" + unit, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    else:
        argv = ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"]
    rc, out = _run(argv, timeout=15)
    if rc != 0:
        return False, "journalctl", (out or "journalctl 执行失败").strip()
    text = out.strip()
    if not text:
        return True, "journalctl", "（暂无日志）"
    return True, "journalctl " + ("_SYSTEMD_USER_UNIT" if scope == "user" else "-u"), text


# ---------------------------------------------------------------- 启停

def _ctl_argv(entry, action):
    """把「项目 + 动作」翻成命令行。项目名来自登记表/自动发现，不是用户现拼的字符串。"""
    if entry["kind"] == "docker":
        return ["docker", action, entry["container"]], None, 90
    if entry["kind"] != "systemd":
        return None, "该项目没有可启停的目标", 0
    if entry["scope"] == "user":
        return ["systemctl", "--user", action, entry["unit"]], None, 90
    return ["sudo", "-n", CTL_HELPER, action, entry["unit"]], None, 90


def _project_by_name(name):
    for entry in _project_index():
        if entry["name"] == name:
            return entry
    return None


def _control(name, action, confirm):
    """执行一次启停。返回 (http 状态码, 结果字典)。"""
    if not CONTROL_ENABLED:
        return 403, {"ok": False, "message": "看板处于只读模式（SYSBOARD_DISABLE_CONTROL=1）"}
    if action not in ("start", "stop", "restart"):
        return 400, {"ok": False, "message": "不支持的动作：%s" % action}
    entry = _project_by_name(name)
    if entry is None:
        return 404, {"ok": False, "message": "没有登记名为 %s 的项目" % name}
    if entry["critical"] and not confirm:
        return 428, {"ok": True, "confirm_required": True,
                     "message": "%s 是关键项目，停它可能让你连不上机器或丢掉当前页面，需要二次确认" % name}
    if entry["kind"] == "systemd":
        user, system = _svc_tables()
        table = user if entry["scope"] == "user" else system
        local_units = _local_units(USER_UNIT_DIR) | _local_units(SYSTEM_UNIT_DIR)
        if not _unit_exists(entry["scope"], entry["unit"], table["files"], local_units):
            return 409, {"ok": False, "message": "本机没有 %s 这个单元" % entry["unit"]}
        if entry["scope"] == "system" and not _helper_ready():
            return 409, {"ok": False, "message": "系统级项目需要先装免密助手：见面板「启用系统级服务控制」弹窗"}
    elif entry["kind"] == "docker":
        info = _docker_info()[0].get(entry["container"])
        if info is None:
            return 409, {"ok": False, "message": "docker 里没有名为 %s 的容器" % entry["container"]}

    argv, err, timeout = _ctl_argv(entry, action)
    if argv is None:
        return 409, {"ok": False, "message": err}

    now = time.monotonic()
    last = _ctl_recent.get(name, 0.0)
    if now - last < 1.5:
        return 429, {"ok": False, "message": "操作太频繁，稍等一下再点"}
    if not _ctl_sem.acquire(blocking=False):
        return 429, {"ok": False, "message": "已有其它项目正在启停，请稍后重试"}
    _ctl_recent[name] = now
    try:
        # start 一个 failed 单元前先清掉失败态，否则可能撞上启动频率限制
        if action == "start" and entry["kind"] == "systemd" and entry["scope"] == "user":
            _run(["systemctl", "--user", "reset-failed", entry["unit"]], timeout=10, env=_user_env())
        env = _user_env() if (entry["kind"] == "systemd" and entry["scope"] == "user") else None
        rc, out = _run(argv, timeout=timeout, env=env)
        message = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if rc != 0:
            message = message or "命令退出码 %d" % rc
        print("[ctl] %s %s rc=%d %s" % (name, action, rc, message), flush=True)
        ok = rc == 0
        # 主动停止后，如果进程没优雅退出（例如 QQAIRobot 收到 SIGTERM 后 exit 130），
        # systemd 会把单元记成 failed。那是"停止方式不优雅"，不是启动失败：
        # 我们明确知道这次停止是自己发起的，直接清掉失败标记，卡片显示「已停止」。
        # reset-failed 对非 failed 单元是空操作，不必先查状态。
        if ok and action == "stop" and entry["kind"] == "systemd":
            if entry["scope"] == "user":
                _run(["systemctl", "--user", "reset-failed", entry["unit"]], timeout=10, env=_user_env())
            else:
                _run(["sudo", "-n", CTL_HELPER, "reset-failed", entry["unit"]], timeout=10)
        # 状态缓存立刻作废并唤醒采集线程，页面下一个周期就能看到新状态
        _svc_cache["time"] = 0.0
        _docker_cache["time"] = 0.0
        _ports_cache["time"] = 0.0
        _wake.set()
        return (200 if ok else 500), {
            "ok": ok, "name": name, "action": action,
            "message": message or ("%s 已执行" % action),
        }
    finally:
        _ctl_sem.release()


def _model():
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return f.read().replace(b"\x00", b"").decode(errors="replace").strip()
    except OSError:
        return ""


def collect():
    disk_rates, net_rates, net_totals, drives = _rates()
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    du = psutil.disk_usage("/")
    procs = _proc_table()
    projects, gone = _project_rows(procs)
    return {
        "time": time.time(),
        "host": {
            "hostname": socket.gethostname(),
            "model": _model(),
            "uptime_sec": max(0, int(time.time() - psutil.boot_time())),
            "load": [round(value, 2) for value in psutil.getloadavg()],
        },
        "cpu": _cpu(),
        "mem": {"total": vm.total, "used": vm.used, "percent": vm.percent},
        "swap": {"total": sm.total, "used": sm.used, "percent": sm.percent},
        "disk": {
            "root": {"total": du.total, "used": du.used, "percent": du.percent},
            **disk_rates,
            "mounts": _mounts(),
            "available": _lsblk_info()[1],
            "drives": drives,
        },
        "net": net_rates,
        # 自开机累计（内核计数器，重启归零）；net_total 只加物理出口网卡，
        # docker0/br-/veth 的流量是从它们转进去的，一起加会算两遍
        "net_totals": net_totals,
        "net_total": _net_total_sum(net_totals),
        "temp": _temp(),
        "procs": _top(procs, "cpu", TOP_PROCESSES),
        "mem_procs": _top(procs, "mem", TOP_PROCESSES),
        "projects": projects,
        "gone_projects": gone,
        "groups": _load_registry()["groups"],
        # 前端靠它决定要不要显示「退出登录」
        "auth": bool(PASSWORD),
        "control": CONTROL_ENABLED,
        # 前端靠它显示「系统服务只读」提示，以及在弹窗里给出安装命令
        "helper": {"ready": _helper_ready(), "install": HELPER_INSTALL,
                   "docker": _docker_info()[1]},
    }


_build_cache = {"key": None, "value": ""}

# 开了 token 后直接访问裸地址时的提示页（不泄露 token 本身，只说怎么带）
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 · 系统看板</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#F0EEE6;color:#141413;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
-webkit-font-smoothing:antialiased}
.box{width:min(360px,92vw);padding:30px 30px 26px;background:#FAF9F5;border:1px solid #E3E1D8;border-radius:16px;
box-shadow:0 12px 30px rgba(20,20,19,.07)}
h1{margin:0 0 6px;font-family:Georgia,"Songti SC",serif;font-size:24px;letter-spacing:1px}
p{margin:0 0 18px;font-size:13px;color:#9C9A93}
label{display:block;margin:0 0 6px;font-size:12px;color:#6B6A66}
input{width:100%;box-sizing:border-box;font:inherit;font-size:15px;padding:10px 12px;
border:1px solid #E3E1D8;border-radius:10px;background:#F0EEE6;color:#141413;outline:none}
input:focus{border-color:#D97757}
button{width:100%;margin-top:16px;font:inherit;font-size:14px;padding:10px 0;border:none;border-radius:10px;
background:#D97757;color:#fff;cursor:pointer;transition:opacity .2s ease}
button:hover{opacity:.9}
button:disabled{opacity:.5;cursor:default}
.err{margin:10px 0 0;font-size:12.5px;color:#C44536;min-height:17px}
</style></head><body><div class="box">
<h1>系统看板</h1>
<p>输入访问密码进入面板</p>
<form id="f">
  <label for="pw">密码</label>
  <input id="pw" name="password" type="password" autocomplete="current-password" autofocus>
  <p class="err" id="err"></p>
  <button type="submit" id="go">登 录</button>
</form>
</div>
<script>
var f=document.getElementById("f"),err=document.getElementById("err"),btn=document.getElementById("go");
f.addEventListener("submit",function(e){
  e.preventDefault();
  var pw=document.getElementById("pw").value;
  if(!pw){err.textContent="请输入密码";return;}
  btn.disabled=true;err.textContent="";
  fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({password:pw, next:new URLSearchParams(location.search).get("next")||""})
  }).then(function(r){return r.json().then(function(j){return {ok:r.ok,body:j};});})
  .then(function(r){
    if(r.ok){location.replace(r.body.next||"/");return;}
    err.textContent=(r.body&&r.body.message)||"密码不对，再试一次";
    btn.disabled=false;document.getElementById("pw").select();
  }).catch(function(){err.textContent="请求失败，检查网络后重试";btn.disabled=false;});
});
</script>
</body></html>
"""


def _build_id():
    """前端构建指纹：三个前端文件内容一变它就变。

    页脚显示的版本号来自页面自身，把它和本函数的值（随 API 下发）比对，
    就能判断浏览器里那个标签页是不是旧页面 —— 这是本会话里反复踩到的坑：
    页面在编辑期间加载过，HTML 和脚本对不上，整块卡片停更且看不出原因。
    """
    global _build_cache
    paths = [os.path.join(STATIC_DIR, name) for name in ("app.js", "style.css", "index.html")]
    try:
        key = tuple((os.path.getmtime(p), os.path.getsize(p)) for p in paths)
    except OSError:
        return ""
    if _build_cache["key"] == key:
        return _build_cache["value"]
    digest = hashlib.sha256()
    for path in paths:
        try:
            with open(path, "rb") as f:
                digest.update(f.read())
        except OSError:
            pass
    _build_cache = {"key": key, "value": digest.hexdigest()[:8]}
    return _build_cache["value"]


class Handler(BaseHTTPRequestHandler):
    server_version = "SysBoard/1.1"
    protocol_version = "HTTP/1.1"
    # 空闲 keep-alive 连接的超时：没有它，一个连上就不说话的客户端会一直占着线程
    timeout = 20
    # 需要鉴权的读取路径（写接口在 do_POST 里单独校验）
    AUTH_PATHS = ("/", "/index.html", "/api/stats", "/api/logs")

    def _send(self, code, body, content_type, head=False, extra=None):
        # 顺手收 str：曾把 401 提示页当 str 传进来，头部发完写 body 才 TypeError，
        # 浏览器只看到一个空 401，很难查
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        # 三个头一起发：no-store 给现代浏览器，Pragma/Expires 兜住老旧客户端和中间代理，
        # 避免再出现"浏览器跑着编辑期间加载的旧页面"
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, code, payload, head=False):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", head)

    def _redirect(self, location, head=False, extra=None):
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _authorized(self):
        """会话 Cookie 或 X-SysBoard-Token 头，二者任一即可；没设密码就放行。

        不再接受 ?token= 查询串：密码会留在浏览器历史、代理日志和 Referer 里。
        """
        if not PASSWORD:
            return True
        if _session_ok(_cookie_value(self.headers.get("Cookie"), SESSION_COOKIE)):
            return True
        return hmac.compare_digest(self.headers.get("X-SysBoard-Token") or "", PASSWORD)

    def _session_cookie(self, value, max_age):
        # HttpOnly 挡 JS 读取；SameSite=Lax 让跨站 POST 根本不带这个 Cookie，
        # 与 _write_trusted 叠成两层，启停接口不会被别的网站借登录态触发
        return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                % (SESSION_COOKIE, value, max_age))

    def _read_json(self):
        """读走 POST 体并解析。

        关键点：无论接受还是拒绝，都必须把 Content-Length 声明的字节读干净。
        否则残留字节会被同一条 keep-alive 连接当成下一个请求行解析（曾实测到
        403 之后紧跟一条 400，日志里请求行是上一段的 JSON）；超过上限的干脆
        读完就断开连接，不让一个超大请求把线程耗住。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return None, "Content-Length 非法"
        if length <= 0:
            return {}, None
        cap = 4096
        raw = self.rfile.read(min(length, cap)) if length else b""
        if length > cap:
            self._drain_body(length - len(raw))
            self.close_connection = True
            return None, "请求体过大"
        try:
            return json.loads(raw or b"{}"), None
        except ValueError:
            return None, "请求体不是合法 JSON"

    def _drain_body(self, remaining):
        """把没读的请求体丢掉（上限 64KB），保证下一条请求从头开始解析。"""
        remaining = min(remaining, 65536)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 4096))
            if not chunk:
                break
            remaining -= len(chunk)

    def _write_trusted(self):
        """写接口的同源校验。

        只靠 Origin 不够：表单型跨站请求同样可以 POST 且常常不带 Origin。这里要求
        1) 浏览器明确告诉我们是同源页面发起（Sec-Fetch-Site），
        2) Origin（若有）与 Host 一致，
        3) Content-Type 必须是 application/json —— 表单能发的三种类型都不满足，
           而带 JSON 头的跨站 fetch 会先撞上 CORS 预检。
        非浏览器客户端（curl/脚本）不带这些头，由 token 兜底。
        """
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin") or ""
        if origin:
            origin_host = (urlparse(origin).hostname or "").lower()
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if origin_host and origin_host not in (host, "127.0.0.1", "localhost"):
                return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def do_GET(self, head=False):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/login":
            # 已经登录的直接送进面板，免得看到登录页发愣
            if self._authorized():
                self._redirect("/", head)
                return
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8", head)
            return
        # 页面和数据接口要登录；静态文件继续免鉴权（子资源请求不带 Cookie 的场景兜底），
        # 它们只是样式和本地 ECharts，不含任何机器信息
        if path in self.AUTH_PATHS and not self._authorized():
            if path in ("/", "/index.html"):
                # 没登录就别甩 401 裸页，直接送登录页；登录成功后回跳原地址
                target = "/login"
                if parsed.query:
                    target += "?next=" + quote("/?" + parsed.query, safe="")
                self._redirect(target, head)
            else:
                self._json(401, {"error": "unauthorized", "login": "/login"}, head)
            return
        if path == "/api/stats":
            stats = _snapshot["stats"]
            if stats is None:
                self._json(503, {"error": "collecting"}, head)
                return
            body = json.dumps(dict(stats, build=_build_id()), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8", head)
            return
        if path == "/api/logs":
            self._logs(query, head)
            return
        if path in ("/", "/index.html"):
            path = "/static/index.html"
        elif path == "/favicon.ico":
            path = "/static/favicon.ico"
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):], head)
            return
        self._json(404, {"error": "not found"}, head)

    def _logs(self, query, head=False):
        name = (query.get("name", [""])[0] or "").strip()
        try:
            lines = int(query.get("tail", ["200"])[0])
        except ValueError:
            lines = 200
        if not NAME_RE.match(name):
            self._json(400, {"ok": False, "message": "项目名非法"}, head)
            return
        entry = _project_by_name(name)
        if entry is None:
            self._json(404, {"ok": False, "message": "没有登记名为 %s 的项目" % name}, head)
            return
        try:
            ok, source, text = _project_logs(entry, lines)
        except Exception as exc:                     # noqa: BLE001 兜底：日志失败不该 500 空响应
            ok, source, text = False, "", "读取日志失败：%s" % exc
        self._json(200 if ok else 502,
                   {"ok": ok, "name": name, "source": source, "text": text, "tail": lines}, head)

    def do_HEAD(self):
        # 探活和监控常用 HEAD，与 GET 同路由，只回头部不带主体
        self.do_GET(head=True)

    def handle_one_request(self):
        # 客户端关掉空闲 keep-alive 连接时会在这里抛 ConnectionResetError，
        # 默认会打印整段堆栈刷屏日志；静默关闭连接即可
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError, socket.timeout):
            self.close_connection = True

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # 先把请求体读干净再做判断：拒绝一个带体的请求也不能把字节留在连接里
        body, err = self._read_json()
        if path not in ("/api/ctl", "/api/disk", "/api/projects", "/login", "/logout"):
            self._json(404, {"error": "not found"})
            return
        if path == "/login":
            self._login(body, err)
            return
        if path == "/logout":
            # 退出也要校验来源：否则别的网站能构造一个跨站 POST 把别人的登录态踢掉
            if not self._authorized() or not self._write_trusted():
                self._json(403, {"ok": False, "message": "请求来源不合法"})
                return
            self._redirect("/login", extra={"Set-Cookie": self._session_cookie("x", 0)})
            return
        # 写接口一律先过登录态与同源校验
        if not self._authorized():
            self._json(401, {"error": "unauthorized", "login": "/login"})
            return
        if not self._write_trusted():
            self._json(403, {"error": "cross origin or bad content type"})
            return
        if err:
            self._json(400, {"error": err})
            return
        if path == "/api/projects":
            # 页面内的卡片编辑：改的是登记表，不经 shell，也不需要 sudo
            code, payload = _edit_projects(body)
            self._json(code, payload)
            return
        if path == "/api/ctl":
            name = str(body.get("name") or "").strip()
            action = str(body.get("action") or "").strip().lower()
            if not NAME_RE.match(name):
                self._json(400, {"ok": False, "message": "项目名非法"})
                return
            code, payload = _control(name, action, bool(body.get("confirm")))
            self._json(code, payload)
            return
        self._disk(body)

    def _login(self, body, err):
        """校验密码并下发签名 Cookie。失败按来源限流，内网也不给人拿字典慢慢试。"""
        ip = self.client_address[0] if self.client_address else "?"
        if not PASSWORD:
            self._json(200, {"ok": True, "next": "/"})
            return
        if _login_blocked(ip):
            self._json(429, {"ok": False, "message": "密码错误次数太多，请等十分钟再试"})
            return
        if not self._write_trusted():
            self._json(403, {"ok": False, "message": "请求来源不合法"})
            return
        if err:
            self._json(400, {"ok": False, "message": err})
            return
        given = str(body.get("password") or "")
        if not hmac.compare_digest(given, PASSWORD):
            _login_record(ip, False)
            print("[login] 密码错误 from %s" % ip, flush=True)
            self._json(401, {"ok": False, "message": "密码不对，再试一次"})
            return
        _login_record(ip, True)
        # 只允许回跳站内路径，挡掉 /login?next=http://evil 这种跳转钓鱼
        nxt = str(body.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        print("[login] 登录成功 from %s" % ip, flush=True)
        self._send(200, json.dumps({"ok": True, "next": nxt}, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8",
                   extra={"Set-Cookie": self._session_cookie(_session_value(), SESSION_TTL)})

    def _disk(self, body):
        action, device = body.get("action"), body.get("device") or ""
        if action not in ("mount", "unmount") or not DEVICE_RE.match(device):
            self._json(400, {"error": "bad action or device"})
            return
        if action == "unmount":
            target = next((m["mount"] for m in _mounts() if m["dev"] == "/dev/" + device), None)
            if target in PROTECTED_MOUNTS:
                self._json(403, {"ok": False, "message": "%s 是系统挂载点，不允许卸载" % target})
                return
        try:
            proc = subprocess.run(["sudo", "-n", DISK_HELPER, action, device],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            msg = {"ok": False, "message": "执行挂载助手失败: %s" % exc}
        else:
            out = (proc.stdout or proc.stderr or "").strip()
            msg = {"ok": proc.returncode == 0, "message": out or ("退出码 %d" % proc.returncode)}
        _lsblk_cache["time"] = 0.0
        self._json(200, msg)

    def _serve_static(self, relative, head=False):
        full = os.path.realpath(os.path.join(STATIC_DIR, relative))
        if not full.startswith(os.path.realpath(STATIC_DIR)) or not os.path.isfile(full):
            self._json(404, {"error": "not found"}, head)
            return
        content_type = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            content_type = "application/javascript"
        compressible = content_type.startswith(("text/", "application/javascript", "application/json",
                                                "image/svg"))
        if content_type.startswith(("text/", "application/javascript", "application/json")):
            content_type += "; charset=utf-8"
        with open(full, "rb") as f:
            body = f.read()
        if relative == "index.html":
            body = body.replace(b"__BUILD__", _build_id().encode())
        extra = None
        # gzip 省流量，但缓存键只能反映"这个文件自己变没变"。
        # index.html 里嵌着 __BUILD__（由 app.js/style.css/index.html 三者共同决定），
        # 只按 index.html 的 mtime 缓存就会在改前端脚本后一直吐旧指纹 —— 页面于是
        # 永远提示"已过期"。所以只给内容自包含的大文件（echarts ~1MB）做缓存，
        # 小文件每次实时压缩（几 KB，开销可忽略）。
        if compressible and len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            cached = None
            if len(body) >= GZIP_CACHE_MIN:
                try:
                    stat = os.stat(full)
                    key = (full, stat.st_mtime_ns, stat.st_size)
                    cached = _gzip_cache.get(key)
                    if cached is None:
                        cached = gzip.compress(body, 6)
                        if len(_gzip_cache) > 16:
                            _gzip_cache.clear()
                        _gzip_cache[key] = cached
                except OSError:
                    cached = None
            if cached is None:
                cached = gzip.compress(body, 6)
            body = cached
            extra = {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
        self._send(200, body, content_type, head, extra)

    def log_message(self, fmt, *args):
        # 2 秒一次的轮询会刷满日志，只保留异常请求。
        # 注意 log_error（连接超时之类）也走这里，那时 args 里没有状态码，
        # 直接取 args[1] 会 IndexError 炸掉整条请求处理，所以先判断长度。
        code = args[1] if len(args) > 1 else None
        # 空闲 keep-alive 到期会走 log_error("Request timed out")，这是正常回收，不是故障
        if code is None and str(fmt).startswith("Request timed out"):
            return
        if code is None or str(code).startswith(("4", "5")):
            super().log_message(fmt, *args)


def _collector():
    # psutil 的 CPU 采样缓存按线程 ID 存储，采集必须固定在同一个线程里做，
    # HTTP 请求线程只读这里产出的快照。
    _prime()
    while True:
        try:
            _snapshot["stats"] = collect()
        except Exception as exc:
            print("collect failed: %s" % exc, flush=True)
        # 启停操作会 set 这个事件，让状态立刻刷新而不是干等一个周期
        _wake.wait(COLLECT_INTERVAL)
        _wake.clear()


def main():
    threading.Thread(target=_collector, name="collector", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print("SysBoard listening on http://%s:%d (control=%s)" % (HOST, PORT, CONTROL_ENABLED), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
