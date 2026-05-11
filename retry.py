"""
Retry - 射击游戏准星覆盖工具
核心方案：Win32 分层窗口 + UpdateLayeredWindow
- 真正像素级透明，准星以外完全不存在
- 鼠标穿透，不抢焦点，不影响游戏
- 控制台面板 + 系统托盘 + 图形化设置
"""

VERSION = "1.1.0"

import tkinter as tk
from tkinter import ttk, colorchooser
import ctypes
import ctypes.wintypes as wt
import sys
import os
import json
import threading
import struct

# ── PyInstaller 路径兼容 ──────────────────────────────────
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(APP_DIR, "config.json")

# ── 可选依赖 ─────────────────────────────────────────────
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

# ============================================================
# Win32 常量 & 结构
# ============================================================
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_TOPMOST     = 0x00000008
WS_EX_NOACTIVATE  = 0x08000000
GWL_EXSTYLE       = -20
GWL_STYLE         = -16
WS_POPUP          = 0x80000000
ULW_ALPHA         = 0x02
AC_SRC_OVER       = 0x00
AC_SRC_ALPHA      = 0x01
HWND_TOPMOST      = -1
SWP_NOMOVE        = 0x0002
SWP_NOSIZE        = 0x0001
SWP_NOACTIVATE    = 0x0010

user32   = ctypes.windll.user32
gdi32    = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp",             ctypes.c_byte),
        ("BlendFlags",          ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat",         ctypes.c_byte),
    ]

# ============================================================
# 默认配置
# ============================================================
DEFAULT_CONFIG = {
    "color":       "#00FF41",
    "dot_color":   "#FF0000",
    "size":        10,
    "thickness":   2,
    "gap":         3,
    "dot_radius":  2,
    "style":       "cross",
}

cfg = dict(DEFAULT_CONFIG)
crosshair_hwnd = None
root           = None
tray_icon      = None


# ============================================================
# 配置读写
# ============================================================
def load_config():
    global cfg
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            cfg.update({k: saved[k] for k in DEFAULT_CONFIG if k in saved})
    except Exception:
        pass

def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ============================================================
# 颜色工具
# ============================================================
def hex_to_bgra(hex_color, alpha=255):
    """#RRGGBB → (B, G, R, A) 元组"""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b, g, r, alpha)


# ============================================================
# 用 GDI 绘制准星到 HBITMAP（真透明 ARGB）
# ============================================================
def render_crosshair_bitmap(c=None):
    """
    返回 (hbmp, width, height)
    HBITMAP 为 32bpp 预乘 alpha DIB
    """
    if c is None:
        c = cfg

    size   = max(1, int(c["size"]))
    thick  = max(1, int(c["thickness"]))
    gap    = max(0, int(c["gap"]))
    dr     = max(0, int(c["dot_radius"]))
    style  = c["style"]
    margin = max(thick + 2, 4)

    if style == "circle":
        radius = size
        w = h = (radius + margin) * 2
    else:
        arm = size + gap
        w = h = (arm + margin) * 2

    cx = cy = w // 2

    # 创建 32bpp DIB Section
    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize",          ctypes.c_uint32),
            ("biWidth",         ctypes.c_int32),
            ("biHeight",        ctypes.c_int32),
            ("biPlanes",        ctypes.c_uint16),
            ("biBitCount",      ctypes.c_uint16),
            ("biCompression",   ctypes.c_uint32),
            ("biSizeImage",     ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_int32),
            ("biYPelsPerMeter", ctypes.c_int32),
            ("biClrUsed",       ctypes.c_uint32),
            ("biClrImportant",  ctypes.c_uint32),
        ]

    bmi = BITMAPINFOHEADER()
    bmi.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.biWidth       = w
    bmi.biHeight      = -h   # top-down
    bmi.biPlanes      = 1
    bmi.biBitCount    = 32
    bmi.biCompression = 0     # BI_RGB

    ppvBits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        None, ctypes.byref(bmi), 0,
        ctypes.byref(ppvBits), None, 0
    )

    # 获取像素缓冲区（BGRA 预乘 alpha）
    buf = (ctypes.c_uint8 * (w * h * 4)).from_address(ppvBits.value)

    # 辅助函数：画像素（预乘 alpha）
    def put_pixel(x, y, r, g, b, a=255):
        if 0 <= x < w and 0 <= y < h:
            idx = (y * w + x) * 4
            # 预乘
            fa = a / 255.0
            buf[idx]     = int(b * fa)
            buf[idx + 1] = int(g * fa)
            buf[idx + 2] = int(r * fa)
            buf[idx + 3] = a

    def parse_hex(hx):
        h2 = hx.lstrip("#")
        return int(h2[0:2], 16), int(h2[2:4], 16), int(h2[4:6], 16)

    # Wu 抗锯齿线段（简化版：直接填矩形，整像素）
    def draw_line_h(y, x0, x1, r, g, b):
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for dy in range(-(thick // 2), thick - thick // 2):
                put_pixel(x, y + dy, r, g, b)

    def draw_line_v(x, y0, y1, r, g, b):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            for dx in range(-(thick // 2), thick - thick // 2):
                put_pixel(x + dx, y, r, g, b)

    def draw_circle(cx_, cy_, radius, r, g, b):
        """Bresenham 圆"""
        x_, y_ = 0, radius
        d = 3 - 2 * radius
        while y_ >= x_:
            for px, py in [
                (cx_ + x_, cy_ + y_), (cx_ - x_, cy_ + y_),
                (cx_ + x_, cy_ - y_), (cx_ - x_, cy_ - y_),
                (cx_ + y_, cy_ + x_), (cx_ - y_, cy_ + x_),
                (cx_ + y_, cy_ - x_), (cx_ - y_, cy_ - x_),
            ]:
                for tt in range(thick):
                    put_pixel(px + tt, py, r, g, b)
                    put_pixel(px - tt, py, r, g, b)
                    put_pixel(px, py + tt, r, g, b)
                    put_pixel(px, py - tt, r, g, b)
            if d < 0:
                d += 4 * x_ + 6
            else:
                d += 4 * (x_ - y_) + 10
                y_ -= 1
            x_ += 1

    cr, cg, cb = parse_hex(c["color"])
    dr2, dg2, db2 = parse_hex(c["dot_color"])

    if style == "cross":
        draw_line_v(cx, cy - gap - size, cy - gap - 1, cr, cg, cb)
        draw_line_v(cx, cy + gap + 1,   cy + gap + size, cr, cg, cb)
        draw_line_h(cy, cx - gap - size, cx - gap - 1, cr, cg, cb)
        draw_line_h(cy, cx + gap + 1,   cx + gap + size, cr, cg, cb)
    elif style == "circle":
        draw_circle(cx, cy, size, cr, cg, cb)

    if dr > 0:
        for dy in range(-dr, dr + 1):
            for dx in range(-dr, dr + 1):
                if dx * dx + dy * dy <= dr * dr:
                    put_pixel(cx + dx, cy + dy, dr2, dg2, db2)

    return hbmp, w, h


# ============================================================
# UpdateLayeredWindow 更新透明窗口
# ============================================================
def update_layered_window(hwnd, hbmp, w, h):
    hdc_screen = user32.GetDC(None)
    hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
    old_bmp    = gdi32.SelectObject(hdc_mem, hbmp)

    blend = BLENDFUNCTION()
    blend.BlendOp             = AC_SRC_OVER
    blend.BlendFlags          = 0
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat         = AC_SRC_ALPHA

    pt_dst  = POINT(0, 0)
    sz_src  = SIZE(w, h)
    pt_src  = POINT(0, 0)

    # 获取窗口当前位置
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pt_dst.x = rect.left
    pt_dst.y = rect.top

    user32.UpdateLayeredWindow(
        hwnd, hdc_screen,
        ctypes.byref(pt_dst),
        ctypes.byref(sz_src),
        hdc_mem,
        ctypes.byref(pt_src),
        0,
        ctypes.byref(blend),
        ULW_ALPHA
    )

    gdi32.SelectObject(hdc_mem, old_bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc_screen)
    gdi32.DeleteObject(hbmp)


# ============================================================
# 准星窗口（纯 Win32，不用 tkinter 子窗口）
# ============================================================
_wnd_class_registered = False

def create_crosshair_window():
    global crosshair_hwnd, _wnd_class_registered

    # 先销毁旧窗口
    if crosshair_hwnd:
        try:
            user32.DestroyWindow(crosshair_hwnd)
        except Exception:
            pass
        crosshair_hwnd = None

    hbmp, w, h = render_crosshair_bitmap()

    # 注册窗口类（只注册一次）
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM
    )

    def wnd_proc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wnd_proc_cb = WNDPROC(wnd_proc)

    class WNDCLASSEX(ctypes.Structure):
        _fields_ = [
            ("cbSize",        ctypes.c_uint),
            ("style",         ctypes.c_uint),
            ("lpfnWndProc",   WNDPROC),
            ("cbClsExtra",    ctypes.c_int),
            ("cbWndExtra",    ctypes.c_int),
            ("hInstance",     wt.HANDLE),
            ("hIcon",         wt.HANDLE),
            ("hCursor",       wt.HANDLE),
            ("hbrBackground", wt.HANDLE),
            ("lpszMenuName",  wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR),
            ("hIconSm",       wt.HANDLE),
        ]

    CLASS_NAME = "RetryXhair"
    if not _wnd_class_registered:
        wc = WNDCLASSEX()
        wc.cbSize        = ctypes.sizeof(WNDCLASSEX)
        wc.style         = 0
        wc.lpfnWndProc   = wnd_proc_cb
        wc.hInstance     = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = CLASS_NAME
        user32.RegisterClassExW(ctypes.byref(wc))
        _wnd_class_registered = True

    # 屏幕中心
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    x  = (sw - w) // 2
    y  = (sh - h) // 2

    ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT |
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE)

    hwnd = user32.CreateWindowExW(
        ex_style, CLASS_NAME, "RetryXhair",
        WS_POPUP,
        x, y, w, h,
        None, None, kernel32.GetModuleHandleW(None), None
    )

    update_layered_window(hwnd, hbmp, w, h)

    user32.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE
    user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
    )

    crosshair_hwnd = hwnd


def refresh_crosshair():
    if root:
        root.after(0, create_crosshair_window)


# ============================================================
# 设置面板
# ============================================================
settings_win = None

def open_settings():
    global settings_win
    if settings_win is not None:
        try:
            if settings_win.winfo_exists():
                settings_win.lift()
                settings_win.focus_force()
                return
        except Exception:
            pass

    win = tk.Toplevel(root)
    win.title("Retry · 准星设置")
    win.resizable(False, False)
    win.configure(bg="#1e1e2e")
    win.wm_attributes("-topmost", True)
    settings_win = win

    FONT   = ("微软雅黑", 10)
    FONT_S = ("微软雅黑", 9)
    BG     = "#1e1e2e"
    FG     = "#cdd6f4"
    ENTRY  = "#313244"
    BTN    = "#45475a"
    ACC    = "#cba6f7"

    local = dict(cfg)

    def lbl(parent, text, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=FG, font=FONT, **kw)

    # 预览（tkinter canvas，仅用于 UI 预览，不影响实际窗口）
    PREV = 120
    pf = tk.Frame(win, bg="#11111b")
    pf.pack(fill="x", padx=16, pady=(16, 4))
    lbl(pf, "预览").pack(anchor="w", padx=8, pady=(4, 0))
    prev_c = tk.Canvas(pf, width=PREV, height=PREV, bg="#000000", highlightthickness=0)
    prev_c.pack(pady=8)

    def rp():
        prev_c.delete("all")
        cx = cy = PREV // 2
        c  = local
        color = c["color"]
        size  = int(c["size"])
        thick = int(c["thickness"])
        gap   = int(c["gap"])
        dr    = int(c["dot_radius"])
        dc    = c["dot_color"]
        style = c["style"]
        if style == "cross":
            prev_c.create_line(cx, cy - gap - size, cx, cy - gap,         fill=color, width=thick)
            prev_c.create_line(cx, cy + gap,         cx, cy + gap + size,  fill=color, width=thick)
            prev_c.create_line(cx - gap - size, cy,  cx - gap,        cy,  fill=color, width=thick)
            prev_c.create_line(cx + gap,         cy, cx + gap + size, cy,  fill=color, width=thick)
        elif style == "circle":
            r = size
            prev_c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=thick)
        if dr > 0:
            prev_c.create_oval(cx - dr, cy - dr, cx + dr, cy + dr, fill=dc, outline="")

    ctrl = tk.Frame(win, bg=BG)
    ctrl.pack(fill="x", padx=16, pady=4)
    row_idx = [0]

    def rl(text):
        lbl(ctrl, text, anchor="w").grid(row=row_idx[0], column=0, sticky="w", pady=3, padx=(0, 10))

    rl("样式")
    style_var = tk.StringVar(value=local["style"])
    ttk.Combobox(ctrl, textvariable=style_var,
                 values=["cross", "circle", "dot_only"],
                 width=10, state="readonly", font=FONT_S
                 ).grid(row=row_idx[0], column=1, sticky="w", pady=3)
    row_idx[0] += 1

    sliders = {}
    def mk_slider(text, key, lo, hi):
        rl(text)
        var = tk.IntVar(value=int(local[key]))
        tk.Scale(ctrl, from_=lo, to=hi, orient="horizontal", variable=var,
                 length=160, bg=BG, fg=FG, troughcolor=ENTRY,
                 highlightthickness=0, activebackground=ACC, font=FONT_S
                 ).grid(row=row_idx[0], column=1, sticky="w", pady=1)
        sliders[key] = var
        row_idx[0] += 1

    mk_slider("臂长",   "size",       1, 40)
    mk_slider("粗细",   "thickness",  1, 8)
    mk_slider("间隙",   "gap",        0, 20)
    mk_slider("中心点", "dot_radius", 0, 8)

    color_swatches = {}
    def mk_color(text, key):
        rl(text)
        fr = tk.Frame(ctrl, bg=BG)
        fr.grid(row=row_idx[0], column=1, sticky="w", pady=3)
        sw = tk.Label(fr, bg=local[key], width=3, relief="solid", bd=1)
        sw.pack(side="left", padx=(0, 6))
        color_swatches[key] = sw
        def pick(k=key, s=sw):
            picked = colorchooser.askcolor(color=local[k], parent=win, title=f"选择{text}")
            if picked and picked[1]:
                local[k] = picked[1]
                s.configure(bg=picked[1])
                rp()
        tk.Button(fr, text="选色", command=pick, bg=BTN, fg=FG,
                  font=FONT_S, relief="flat", padx=6).pack(side="left")
        row_idx[0] += 1

    mk_color("准星颜色",   "color")
    mk_color("中心点颜色", "dot_color")

    def on_change(*_):
        local["style"]      = style_var.get()
        local["size"]       = sliders["size"].get()
        local["thickness"]  = sliders["thickness"].get()
        local["gap"]        = sliders["gap"].get()
        local["dot_radius"] = sliders["dot_radius"].get()
        rp()

    style_var.trace_add("write", on_change)
    for v in sliders.values():
        v.trace_add("write", on_change)
    rp()

    bf = tk.Frame(win, bg=BG)
    bf.pack(fill="x", padx=16, pady=(8, 16))

    def apply_all():
        cfg.update(local)
        save_config()
        refresh_crosshair()

    def reset_all():
        local.update(DEFAULT_CONFIG)
        style_var.set(local["style"])
        for k, v in sliders.items():
            v.set(int(local[k]))
        for k, sw in color_swatches.items():
            sw.configure(bg=local[k])
        rp()

    tk.Button(bf, text="✔ 应用", command=apply_all,
              bg="#a6e3a1", fg="#1e1e2e", font=FONT, relief="flat",
              padx=16, pady=4).pack(side="left", padx=(0, 8))
    tk.Button(bf, text="↩ 重置默认", command=reset_all,
              bg=BTN, fg=FG, font=FONT, relief="flat",
              padx=12, pady=4).pack(side="left")

    win.update_idletasks()
    ww, wh = win.winfo_width(), win.winfo_height()
    sw2, sh2 = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw2 - ww) // 2}+{(sh2 - wh) // 2}")


# ============================================================
# 控制台面板
# ============================================================
def create_console():
    root.title(f"Retry v{VERSION}")
    root.configure(bg="#0d0d0d")
    root.resizable(False, False)
    root.wm_attributes("-topmost", False)
    root.overrideredirect(False)

    # 窗口图标
    # 打包后 ico 在 _MEIPASS 内；开发时在源码目录
    _meipass = getattr(sys, "_MEIPASS", None)
    _ico_bundled = os.path.join(_meipass, "icon.ico") if _meipass else None
    _ico_local   = os.path.join(APP_DIR, "icon.ico")
    _png_local   = os.path.join(APP_DIR, "icon.png")

    try:
        if _ico_bundled and os.path.exists(_ico_bundled):
            root.iconbitmap(_ico_bundled)
        elif os.path.exists(_ico_local):
            root.iconbitmap(_ico_local)
        elif os.path.exists(_png_local) and HAS_TRAY:
            from PIL import ImageTk
            _img   = Image.open(_png_local).resize((32, 32))
            _photo = ImageTk.PhotoImage(_img)
            root.iconphoto(True, _photo)
    except Exception:
        pass

    # ── 配色 ────────────────────────────────────────────────
    BG        = "#0d0d0d"   # 主背景（近黑）
    BG2       = "#161616"   # 卡片背景
    BORDER    = "#2a2a2a"   # 分割线
    FG        = "#e8e8e8"   # 主文字
    FG2       = "#555555"   # 次要文字
    ACC       = "#00ff41"   # 品牌绿（准星颜色）
    RED       = "#ff4455"   # 退出红

    W = 360
    H = 270

    # ── 标题栏（自定义，可拖动） ─────────────────────────────
    title_bar = tk.Frame(root, bg=BG, height=36)
    title_bar.pack(fill="x")
    title_bar.pack_propagate(False)

    tk.Label(title_bar, text=f"● Retry  v{VERSION}",
             font=("Consolas", 11, "bold"), bg=BG, fg=ACC
             ).pack(side="left", padx=14, pady=8)

    # 关闭按钮（标题栏右上角）
    def _close_btn_enter(e):  close_btn.configure(bg="#3a1a1a", fg=RED)
    def _close_btn_leave(e):  close_btn.configure(bg=BG, fg=FG2)
    close_btn = tk.Label(title_bar, text="✕", font=("Consolas", 11),
                         bg=BG, fg=FG2, cursor="hand2", padx=12)
    close_btn.pack(side="right")
    close_btn.bind("<Button-1>", do_quit)
    close_btn.bind("<Enter>", _close_btn_enter)
    close_btn.bind("<Leave>", _close_btn_leave)

    # 拖动
    _drag = {"x": 0, "y": 0}
    def _drag_start(e):
        _drag["x"] = e.x_root - root.winfo_x()
        _drag["y"] = e.y_root - root.winfo_y()
    def _drag_move(e):
        root.geometry(f"+{e.x_root - _drag['x']}+{e.y_root - _drag['y']}")
    title_bar.bind("<ButtonPress-1>",   _drag_start)
    title_bar.bind("<B1-Motion>",       _drag_move)

    # ── 分割线 ───────────────────────────────────────────────
    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    # ── 状态卡片 ─────────────────────────────────────────────
    card = tk.Frame(root, bg=BG2, padx=18, pady=14)
    card.pack(fill="x", padx=14, pady=(14, 8))

    dot_c = tk.Label(card, text="◉", font=("Consolas", 12),
                     bg=BG2, fg=ACC)
    dot_c.grid(row=0, column=0, sticky="w")
    tk.Label(card, text="准星已激活",
             font=("微软雅黑", 10, "bold"), bg=BG2, fg=FG
             ).grid(row=0, column=1, sticky="w", padx=(8, 0))

    tk.Frame(card, bg=BORDER, height=1).grid(
        row=1, column=0, columnspan=2, sticky="ew", pady=(10, 8))

    tk.Label(card, text="样式",
             font=("微软雅黑", 9), bg=BG2, fg=FG2
             ).grid(row=2, column=0, sticky="w")

    status_style = tk.Label(card, text=cfg.get("style", "cross"),
                             font=("Consolas", 9), bg=BG2, fg=ACC)
    status_style.grid(row=2, column=1, sticky="e")

    tk.Label(card, text="颜色",
             font=("微软雅黑", 9), bg=BG2, fg=FG2
             ).grid(row=3, column=0, sticky="w", pady=(4, 0))

    color_dot = tk.Label(card, text="██",
                          font=("Consolas", 10), bg=BG2,
                          fg=cfg.get("color", "#00FF41"))
    color_dot.grid(row=3, column=1, sticky="e", pady=(4, 0))

    card.columnconfigure(1, weight=1)

    # 刷新状态显示
    def refresh_status():
        status_style.configure(text=cfg.get("style", "cross"))
        color_dot.configure(fg=cfg.get("color", "#00FF41"))

    # ── 按钮区 ───────────────────────────────────────────────
    btn_frame = tk.Frame(root, bg=BG)
    btn_frame.pack(fill="x", padx=14, pady=(0, 8))

    def make_btn(parent, text, cmd, bg_n, bg_h, fg_n, fg_h):
        b = tk.Label(parent, text=text, font=("微软雅黑", 10),
                     bg=bg_n, fg=fg_n, cursor="hand2",
                     padx=0, pady=8, relief="flat")
        b.bind("<Enter>",    lambda e: b.configure(bg=bg_h, fg=fg_h))
        b.bind("<Leave>",    lambda e: b.configure(bg=bg_n, fg=fg_n))
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def _open_settings_and_refresh():
        open_settings()
        root.after(300, refresh_status)

    btn_settings = make_btn(btn_frame,
        "⚙  设置准星",
        _open_settings_and_refresh,
        bg_n="#1a2e1a", bg_h="#204020",
        fg_n=ACC,       fg_h="#ffffff")
    btn_settings.pack(side="left", fill="x", expand=True, padx=(0, 6))

    btn_quit = make_btn(btn_frame,
        "退出",
        do_quit,
        bg_n="#2a1010", bg_h="#3d1515",
        fg_n=RED,        fg_h="#ffffff")
    btn_quit.pack(side="left", ipadx=14)

    # ── 底部提示 ─────────────────────────────────────────────
    tk.Label(root, text="F1 / Esc  退出",
             font=("Consolas", 8), bg=BG, fg=FG2
             ).pack(pady=(0, 10))

    # ── 定位居中 ─────────────────────────────────────────────
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2 + 40}")

    # ── 首次启动免责声明 ──────────────────────────────────────
    _disclaimer_flag = os.path.join(APP_DIR, ".disclaimer_shown")
    if not os.path.exists(_disclaimer_flag):
        def _show_disclaimer():
            # 延迟到主循环稳定后再创建弹窗，避免 grab_set 卡死
            root.update()

            dlg = tk.Toplevel(root)
            dlg.title("使用须知")
            dlg.configure(bg="#0d0d0d")
            dlg.resizable(False, False)
            dlg.wm_attributes("-topmost", True)
            # 不调用 grab_set / transient，改用 protocol 阻止直接关闭
            dlg.protocol("WM_DELETE_WINDOW", lambda: None)

            _sw = root.winfo_screenwidth()
            _sh = root.winfo_screenheight()
            DW, DH = 360, 230
            dlg.geometry(f"{DW}x{DH}+{(_sw - DW) // 2}+{(_sh - DH) // 2}")

            # 标题
            tk.Label(dlg, text="⚠  使用须知",
                     font=("微软雅黑", 11, "bold"),
                     bg="#0d0d0d", fg="#ffcc00"
                     ).pack(pady=(20, 8))

            # 分割线
            tk.Frame(dlg, bg="#2a2a2a", height=1).pack(fill="x", padx=20)

            # 正文
            tk.Label(dlg,
                     text=(
                         "Retry 可能在某些游戏中属于非法外挂，\n"
                         "请在使用前自行确认该游戏的相关规定。\n\n"
                         "因使用 Retry 导致的任何封号、处罚\n"
                         "或其他问题，Retry 团队概不负责。"
                     ),
                     font=("微软雅黑", 9),
                     bg="#0d0d0d", fg="#aaaaaa",
                     justify="center", wraplength=300
                     ).pack(pady=(12, 14))

            # 按钮（点击后才能关闭）
            def _confirm():
                try:
                    open(_disclaimer_flag, "w").close()
                except Exception:
                    pass
                dlg.destroy()

            tk.Button(dlg, text="我已知晓，继续使用",
                      command=_confirm,
                      bg="#00ff41", fg="#0d0d0d",
                      font=("微软雅黑", 9, "bold"),
                      relief="flat", padx=18, pady=7,
                      cursor="hand2"
                      ).pack(pady=(0, 20))

        root.after(400, _show_disclaimer)




# ============================================================
# 系统托盘
# ============================================================
def build_tray_icon():
    if not HAS_TRAY:
        return None
    sz  = 64
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    cx  = cy = sz // 2
    c   = (0, 255, 65, 255)
    d.line([(cx, 4),      (cx, cy - 6)], fill=c, width=3)
    d.line([(cx, cy + 6), (cx, sz - 4)], fill=c, width=3)
    d.line([(4, cy),      (cx - 6, cy)], fill=c, width=3)
    d.line([(cx + 6, cy), (sz - 4, cy)], fill=c, width=3)
    d.ellipse([(cx - 3, cy - 3), (cx + 3, cy + 3)], fill=(255, 0, 0, 255))
    menu = pystray.Menu(
        pystray.MenuItem("设置准星",   lambda icon, item: root.after(0, open_settings)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出 Retry", lambda icon, item: root.after(0, do_quit)),
    )
    return pystray.Icon("Retry", img, "Retry 准星", menu)


# ============================================================
# 退出
# ============================================================
def do_quit(event=None):
    global tray_icon, crosshair_hwnd
    try:
        if crosshair_hwnd:
            user32.DestroyWindow(crosshair_hwnd)
            crosshair_hwnd = None
    except Exception:
        pass
    try:
        if tray_icon:
            tray_icon.stop()
    except Exception:
        pass
    try:
        root.quit()
        root.destroy()
    except Exception:
        pass
    sys.exit(0)


# ============================================================
# 主入口
# ============================================================
def main():
    global root, tray_icon

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    load_config()

    root = tk.Tk()
    root.withdraw()

    create_crosshair_window()

    root.bind_all("<F1>",     do_quit)
    root.bind_all("<Escape>", do_quit)

    if HAS_TRAY:
        tray_icon = build_tray_icon()
        if tray_icon:
            threading.Thread(target=tray_icon.run, daemon=True).start()

    root.deiconify()
    create_console()
    root.mainloop()


if __name__ == "__main__":
    main()
