#!/usr/bin/env python3
"""生成 SysBoard 网站图标（Claude 主题：象牙白圆角底 + 珊瑚色环形进度）。

机器上没有 PIL/ImageMagick，这里用标准库手写 PNG 与 ICO 编码。
输出到 static/：favicon.svg、favicon.ico（16/32/48/64 多尺寸）、preview-256.png。
用法：python3 tools/make_favicon.py
"""

import math
import os
import struct
import zlib

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# 与 style.css 的设计变量保持一致
IVORY = (240, 238, 230)
BORDER = (227, 225, 216)
CORAL = (217, 119, 87)
TRACK = (227, 225, 216)

CORNER_R = 0.24          # 圆角半径（相对画布边长）
RING_MID = 0.30          # 环中线半径
RING_HALF = 0.0703       # 环半宽
ARC_FRACTION = 0.72      # 珊瑚色弧覆盖比例
SUPERSAMPLE = 4          # 每像素 4x4 子采样做抗锯齿


def _mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _in_rounded_rect(x, y, r):
    """点到圆角正方形容器的距离，返回是否在内。"""
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0


def _rounded_rect_edge(x, y, r, edge):
    """是否落在圆角矩形边框带内（用于描边）。"""
    cx = min(max(x, r), 1.0 - r)
    cy = min(max(y, r), 1.0 - r)
    d = math.hypot(x - cx, y - cy)
    if x < 0 or x > 1 or y < 0 or y > 1:
        return False
    # 直边区域：到最近的边距离
    straight = min(x, y, 1.0 - x, 1.0 - y)
    inside_r = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
    if not inside_r:
        return False
    if x < r or x > 1 - r or y < r or y > 1 - r:
        return straight <= edge
    return d >= r - edge


def _sample(x, y):
    """单个子采样点的颜色，返回 (r,g,b,a)。"""
    if not _in_rounded_rect(x, y, CORNER_R):
        return (0, 0, 0, 0)
    if _rounded_rect_edge(x, y, CORNER_R, 0.028):
        return BORDER + (255,)

    dx, dy = x - 0.5, y - 0.5
    d = math.hypot(dx, dy)

    # 环：到中线距离在环半宽内
    if abs(d - RING_MID) <= RING_HALF:
        ang = math.atan2(dy, dx)          # 0 = 右，-pi/2 = 上
        frac = ((ang + math.pi / 2) % (2 * math.pi)) / (2 * math.pi)  # 从正上方顺时针 0..1
        if frac <= ARC_FRACTION:
            return CORAL + (255,)
        # 端帽：让珊瑚色弧的两端是圆头
        for cap_frac in (0.0, ARC_FRACTION):
            ca = cap_frac * 2 * math.pi - math.pi / 2
            cxp = 0.5 + RING_MID * math.cos(ca)
            cyp = 0.5 + RING_MID * math.sin(ca)
            if math.hypot(x - cxp, y - cyp) <= RING_HALF:
                return CORAL + (255,)
        return TRACK + (255,)

    return IVORY + (255,)


def render(size):
    """渲染成 RGBA 行数据，超采样抗锯齿。"""
    rows = []
    step = 1.0 / (size * SUPERSAMPLE)
    half = step / 2
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    x = (px * SUPERSAMPLE + sx) * step + half
                    y = (py * SUPERSAMPLE + sy) * step + half
                    c = _sample(x, y)
                    for i in range(4):
                        acc[i] += c[i]
            n = SUPERSAMPLE * SUPERSAMPLE
            row += bytes(round(v / n) for v in acc)
        rows.append(bytes(row))
    return rows


def png_bytes(size, rows):
    """手写 PNG（8 位 RGBA，无隔行）。"""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(entries):
    """手写 ICO，内嵌 PNG（现代浏览器均支持）。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    dir_entries, blobs = b"", b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        dir_entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return header + dir_entries + blobs


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="32" height="32">
  <title>系统看板</title>
  <rect x="0.5" y="0.5" width="31" height="31" rx="7.5" fill="#F0EEE6" stroke="#E3E1D8"/>
  <circle cx="16" cy="16" r="9.6" fill="none" stroke="#E3E1D8" stroke-width="4.5"/>
  <path d="M 16 6.4 A 9.6 9.6 0 1 1 6.57 17.80" fill="none" stroke="#D97757"
        stroke-width="4.5" stroke-linecap="round"/>
</svg>
"""


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    entries = []
    for size in (16, 32, 48, 64):
        entries.append((size, png_bytes(size, render(size))))
    with open(os.path.join(OUT_DIR, "favicon.ico"), "wb") as f:
        f.write(ico_bytes(entries))
    # 页面里显式引用的一张 PNG，供不支持 SVG 图标的浏览器使用
    with open(os.path.join(OUT_DIR, "favicon-32.png"), "wb") as f:
        f.write(png_bytes(32, render(32)))
    with open(os.path.join(OUT_DIR, "favicon.svg"), "w") as f:
        f.write(SVG)
    # 大图仅供人工核对构图，不参与线上访问
    preview = os.path.join(os.path.dirname(os.path.abspath(__file__)), "favicon-preview.png")
    with open(preview, "wb") as f:
        f.write(png_bytes(256, render(256)))
    print("生成完成：static/favicon.svg、static/favicon.ico（16/32/48/64）、static/favicon-32.png")
    print("人工核对用大图：tools/favicon-preview.png")


if __name__ == "__main__":
    main()