"""GameSay · 游戏快捷语录助手

在各类游戏里一键随机喊话（全自动）：选择词库（按文件名）→ 自定义发送流程
（打开聊天框按键 / 发送方式 / 粘贴快捷键 / 各步延迟）→ 按全局热键自动执行：
先按「打开聊天框」键 → 写入剪贴板 → 粘贴 → 按需回车发送。

原理：SendInput 模拟按键（字母数字用扫描码，兼容 DirectInput 游戏），
游戏全屏/前台均有效，全程无需切出窗口。

依赖：customtkinter（启动脚本自动安装）、Python 标准库。
"""

import json
import os
import queue
import random
import shutil
import sys
import threading
import time
import winsound
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import tkinter.messagebox as messagebox

import keysim
from hotkey import HotkeyListener, KEY_CHOICES, MODIFIER_CHOICES

# ---- 路径：兼容源码运行与 PyInstaller 打包运行 ----
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # exe 所在目录（用户可写，词库释放到这里）
    ROOT = Path(sys.executable).resolve().parent
    # PyInstaller 解包的内置资源目录
    BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent
    BUNDLE = ROOT

DATA_DIR = ROOT / "data"
LIB_DIR = ROOT / "libraries"
CONFIG_FILE = ROOT / "config.json"


def ensure_resources() -> None:
    """打包版首次运行：把内置词库释放到 exe 旁边（可编辑、可扩展）。"""
    if not FROZEN:
        return
    builtin = BUNDLE / "data" / "游戏语录.txt"
    if builtin.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        dst = DATA_DIR / "游戏语录.txt"
        if not dst.exists():
            shutil.copy2(builtin, dst)
    src_lib = BUNDLE / "libraries"
    if src_lib.exists():
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        for f in src_lib.glob("*.txt"):
            dst = LIB_DIR / f.name
            if not dst.exists():
                shutil.copy2(f, dst)

APP_NAME = "GameSay"
APP_SUB = "游戏快捷语录 · 一键随机喊话（全自动）"

# ---- 暗色电竞配色 ----
BG = "#0b0e14"
PANEL = "#12161f"
PANEL_HOVER = "#1a2029"
FIELD_BG = "#0f141d"
BORDER = "#232b3a"
TEXT = "#e8eef5"
MUTED = "#7d8a99"
ACCENT = "#00d4ff"
ACCENT_DARK = "#0e9fd8"
SUCCESS = "#2ecc71"
DANGER = "#ff5c5c"
WARN = "#ffb454"

FONT = ("Microsoft YaHei UI", 11)
FONT_SMALL = ("Microsoft YaHei UI", 10)
FONT_TITLE = ("Microsoft YaHei UI", 24, "bold")
FONT_SECTION = ("Microsoft YaHei UI", 13, "bold")
FONT_PREVIEW = ("Microsoft YaHei UI", 16)
FONT_MONO = ("Consolas", 10)

FLOW_MODES = ("仅复制", "粘贴", "粘贴并回车")
PASTE_COMBOS = {"Ctrl+V": ("Ctrl",), "Ctrl+Shift+V": ("Ctrl", "Shift")}
# 打开聊天框按键：常见游戏快捷键优先
CHAT_KEYS = ["无", "回车", "T", "Y", "~", "/", "空格"] + [f"F{i}" for i in range(1, 13)]


def is_admin() -> bool:
    """当前进程是否以管理员权限运行。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class GameSayApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=BG)
        ensure_resources()
        self.title(f"{APP_NAME} · 游戏快捷语录")
        self.geometry("1160x840")
        self.minsize(980, 720)

        self.libraries: dict[str, dict] = {}   # 文件名 -> {"items": [...], "builtin": bool}
        self.current_phrase = ""
        self.sending = False
        self.looping = False
        self.stop_event = threading.Event()
        self.ui_queue: queue.Queue = queue.Queue()
        self.hotkey = HotkeyListener(on_press=self._hotkey_pressed)

        self._build_ui()
        self._load_libraries()
        self._load_config()
        self.after(100, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if not is_admin():
            self._log(
                "[提示] 当前为普通权限：若游戏以管理员身份运行，请用「以管理员身份运行」启动本工具，"
                "否则按键无法注入游戏（启动脚本已支持自动提权）",
                "#ffb454",
            )
            self._set_status("普通权限运行中（游戏若管理员运行请提权）", WARN)

    # ---------------------------------------------------------------- UI 线程
    def _drain_ui_queue(self):
        try:
            while True:
                callback = self.ui_queue.get_nowait()
                callback()
        except queue.Empty:
            pass
        self.after(100, self._drain_ui_queue)

    def _ui(self, callback):
        self.ui_queue.put(callback)

    # ---------------------------------------------------------------- 界面
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=26, pady=(20, 10))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(header, text="🎮", font=("Segoe UI Emoji", 30)).grid(row=0, column=0, rowspan=2, padx=(0, 12))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=1, rowspan=2, sticky="w")
        ctk.CTkLabel(title_box, text=APP_NAME, font=FONT_TITLE, text_color=ACCENT).pack(anchor="w")
        ctk.CTkLabel(title_box, text=APP_SUB, font=FONT_SMALL, text_color=MUTED).pack(anchor="w", pady=(2, 0))

        self.header_badge = ctk.CTkLabel(
            header, text="● 热键未启用", font=FONT_SMALL,
            text_color=MUTED, fg_color=PANEL, corner_radius=14, padx=14, pady=6,
        )
        self.header_badge.grid(row=0, column=2, rowspan=2, sticky="e")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=26, pady=(0, 14))
        body.grid_columnconfigure(0, weight=0, minsize=340)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_lib_panel(body)
        self._build_send_panel(body)
        self._build_footer()

    def _panel(self, parent):
        return ctk.CTkFrame(
            parent, fg_color=PANEL, corner_radius=16,
            border_width=1, border_color=BORDER,
        )

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=FONT_SECTION, text_color=TEXT).pack(
            anchor="w", padx=20, pady=(18, 12)
        )

    def _build_lib_panel(self, body):
        panel = self._panel(body)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._section(panel, "📚  词库选择")

        ctk.CTkLabel(
            panel, text="选择一个词库（按文件名显示）", font=FONT_SMALL, text_color=MUTED,
        ).pack(anchor="w", padx=20, pady=(0, 6))
        self.library_combo = ctk.CTkComboBox(
            panel, values=[], height=38, corner_radius=8,
            fg_color=FIELD_BG, border_color=BORDER, button_color=ACCENT_DARK,
            button_hover_color=ACCENT, dropdown_fg_color=PANEL,
            dropdown_hover_color=PANEL_HOVER, dropdown_text_color=TEXT,
            font=FONT, text_color=TEXT, state="readonly",
            command=lambda _n: self._update_count(),
        )
        self.library_combo.pack(fill="x", padx=20, pady=(0, 8))

        self.lib_info = ctk.CTkLabel(
            panel, text="", font=FONT_SMALL, text_color=ACCENT, wraplength=290, justify="left",
        )
        self.lib_info.pack(anchor="w", padx=20, pady=(0, 10))

        tip = ctk.CTkFrame(panel, fg_color=FIELD_BG, corner_radius=10, border_width=1, border_color=BORDER)
        tip.pack(fill="x", padx=20, pady=(0, 12))
        ctk.CTkLabel(
            tip, text="💡 自定义词库\n把 .txt 文件放进 libraries 文件夹\n每行一句，点「刷新」即可载入",
            font=FONT_SMALL, text_color=MUTED, wraplength=280, justify="left",
        ).pack(anchor="w", padx=14, pady=12)

        buttons = ctk.CTkFrame(panel, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(
            buttons, text="🔄 刷新", command=self._reload_libraries, width=72, height=32,
            corner_radius=8, fg_color=PANEL_HOVER, hover_color="#242c3a",
            text_color=TEXT, font=FONT_SMALL,
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="📁 词库文件夹", command=lambda: os.startfile(LIB_DIR), width=110, height=32,
            corner_radius=8, fg_color="transparent", border_width=1, border_color=BORDER,
            hover_color=PANEL_HOVER, text_color=ACCENT, font=FONT_SMALL,
        ).pack(side="right")

    def _build_send_panel(self, body):
        panel = self._panel(body)
        panel.grid(row=0, column=1, sticky="nsew")
        self._section(panel, "📣  喊话设置")

        # ---- 喊话预览卡片 ----
        preview = ctk.CTkFrame(
            panel, fg_color=FIELD_BG, corner_radius=12, border_width=1, border_color=BORDER,
        )
        preview.pack(fill="x", padx=20, pady=(0, 12))
        self.preview_label = ctk.CTkLabel(
            preview, text="点「换一条」随机抽取一句，或直接按热键", font=FONT_PREVIEW,
            text_color=TEXT, wraplength=640, justify="left",
        )
        self.preview_label.pack(fill="x", padx=18, pady=(16, 4))
        preview_btns = ctk.CTkFrame(preview, fg_color="transparent")
        preview_btns.pack(fill="x", padx=12, pady=(4, 12))
        ctk.CTkButton(
            preview_btns, text="🎲 换一条", command=self.reroll, height=32, corner_radius=8,
            fg_color="transparent", border_width=1, border_color=ACCENT_DARK,
            hover_color=PANEL_HOVER, text_color=ACCENT, font=FONT_SMALL,
        ).pack(side="left")
        ctk.CTkButton(
            preview_btns, text="📋 复制这条", command=self.copy_current, height=32, corner_radius=8,
            fg_color=PANEL_HOVER, hover_color="#242c3a", text_color=TEXT, font=FONT_SMALL,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            preview_btns, text="🚀 发送这条", command=self.send_current, height=32, corner_radius=8,
            fg_color=ACCENT_DARK, hover_color=ACCENT, text_color="#06121a",
            font=(FONT_SMALL[0], 10, "bold"),
        ).pack(side="right")

        # ---- 发送流程（全自动，可自定义） ----
        flow = ctk.CTkFrame(panel, fg_color="transparent")
        flow.pack(fill="x", padx=20, pady=(0, 12))
        for col in range(3):
            flow.grid_columnconfigure(col, weight=1)

        self.chat_key = ctk.StringVar(value="回车")
        self.chat_delay = ctk.StringVar(value="400")
        self.flow_mode = ctk.StringVar(value=FLOW_MODES[2])
        self.paste_combo = ctk.StringVar(value="Ctrl+V")
        self.enter_delay = ctk.StringVar(value="150")
        self.loop_interval = ctk.StringVar(value="2000")

        def flow_field(row, col, label, widget):
            box = ctk.CTkFrame(flow, fg_color="transparent")
            box.grid(row=row, column=col, sticky="ew", padx=(0 if col == 0 else 8, 0))
            ctk.CTkLabel(box, text=label, font=FONT_SMALL, text_color=MUTED).pack(anchor="w", pady=(0, 4))
            widget(box).pack(fill="x")

        def combo(box, variable, values, width=None):
            kw = dict(
                variable=variable, values=values, height=32, corner_radius=8,
                fg_color=FIELD_BG, border_color=BORDER, button_color=ACCENT_DARK,
                button_hover_color=ACCENT, dropdown_fg_color=PANEL, dropdown_hover_color=PANEL_HOVER,
                dropdown_text_color=TEXT, font=FONT_SMALL, text_color=TEXT, state="readonly",
            )
            if width:
                kw["width"] = width
            return ctk.CTkComboBox(box, **kw)

        def entry(box, variable):
            return ctk.CTkEntry(
                box, textvariable=variable, height=32, corner_radius=8,
                fg_color=FIELD_BG, border_color=BORDER, text_color=TEXT, font=FONT_SMALL,
            )

        flow_field(0, 0, "打开聊天框按键", lambda box: combo(box, self.chat_key, CHAT_KEYS))
        flow_field(0, 1, "聊天框延迟（毫秒）", lambda box: entry(box, self.chat_delay))
        flow_field(0, 2, "发送方式", lambda box: combo(box, self.flow_mode, FLOW_MODES))
        flow_field(1, 0, "粘贴快捷键", lambda box: combo(box, self.paste_combo, list(PASTE_COMBOS.keys())))
        flow_field(1, 1, "回车延迟（毫秒）", lambda box: entry(box, self.enter_delay))
        flow_field(1, 2, "循环间隔（毫秒）", lambda box: entry(box, self.loop_interval))

        flow_tip = ctk.CTkLabel(
            panel,
            text="全自动流程：按热键开始循环 → 每句自动「打开聊天框 → 粘贴随机语录 → 回车发送」→ 再按热键停止。"
                 "不同游戏打开聊天的键不同（如 回车 / T / Y / ~），请在游戏里确认后设置；循环间隔建议 ≥ 1500ms。",
            font=FONT_SMALL, text_color=MUTED, wraplength=700, justify="left",
        )
        flow_tip.pack(anchor="w", padx=20, pady=(0, 12))

        # ---- 全局热键 ----
        hot = ctk.CTkFrame(panel, fg_color=FIELD_BG, corner_radius=12, border_width=1, border_color=BORDER)
        hot.pack(fill="x", padx=20, pady=(0, 12))
        row1 = ctk.CTkFrame(hot, fg_color="transparent")
        row1.pack(fill="x", padx=16, pady=(12, 4))
        self.hotkey_enabled = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            row1, text="全局热键开关", variable=self.hotkey_enabled,
            command=self._apply_hotkey, progress_color=ACCENT, fg_color=PANEL_HOVER, font=FONT_SMALL,
        ).pack(side="left")
        self.mod_ctrl = ctk.BooleanVar(value=False)
        self.mod_alt = ctk.BooleanVar(value=False)
        self.mod_shift = ctk.BooleanVar(value=False)
        for var, name in ((self.mod_ctrl, "Ctrl"), (self.mod_alt, "Alt"), (self.mod_shift, "Shift")):
            ctk.CTkCheckBox(
                row1, text=name, variable=var, command=self._apply_hotkey,
                fg_color=ACCENT_DARK, hover_color=ACCENT, border_color=BORDER,
                text_color=TEXT, font=FONT_SMALL, checkbox_width=18, checkbox_height=18,
            ).pack(side="left", padx=(14, 0))
        self.hotkey_key = ctk.StringVar(value="F8")
        ctk.CTkComboBox(
            row1, variable=self.hotkey_key, values=KEY_CHOICES, width=96, height=30,
            corner_radius=8, fg_color=PANEL, border_color=BORDER,
            button_color=ACCENT_DARK, button_hover_color=ACCENT,
            dropdown_fg_color=PANEL, dropdown_hover_color=PANEL_HOVER,
            dropdown_text_color=TEXT, font=FONT_SMALL, text_color=TEXT, state="readonly",
            command=lambda _k: self._apply_hotkey(),
        ).pack(side="left", padx=(12, 0))
        self.loop_btn = ctk.CTkButton(
            row1, text="🔁 开始循环", command=self._toggle_loop, width=112, height=30,
            corner_radius=8, fg_color="#1d3d3a", hover_color="#27504c",
            text_color="#7ff5d8", font=FONT_SMALL,
        )
        self.loop_btn.pack(side="right")
        self.hotkey_tip = ctk.CTkLabel(
            hot, text="按热键开始循环喊话，再按一次停止（开关均有提示音）；建议 Ctrl / Alt / Shift + F键，避免与游戏按键冲突",
            font=FONT_SMALL, text_color=MUTED, wraplength=700, justify="left",
        )
        self.hotkey_tip.pack(anchor="w", padx=16, pady=(2, 12))

        # ---- 发送记录 ----
        log_head = ctk.CTkFrame(panel, fg_color="transparent")
        log_head.pack(fill="x", padx=20, pady=(6, 6))
        ctk.CTkLabel(log_head, text="📜 发送记录", font=FONT_SMALL, text_color=MUTED).pack(side="left")
        ctk.CTkButton(
            log_head, text="清空", command=lambda: self.log.delete("0.0", "end"),
            width=56, height=24, corner_radius=6, fg_color=PANEL_HOVER,
            hover_color="#242c3a", text_color=MUTED, font=FONT_SMALL,
        ).pack(side="right")

        self.log = ctk.CTkTextbox(
            panel, fg_color="#0a0e15", text_color="#c8d4e0", corner_radius=10,
            border_width=1, border_color=BORDER, font=FONT_MONO, wrap="word",
            height=150,
        )
        self.log.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        self.status = ctk.CTkLabel(panel, text="就绪", font=FONT_SMALL, text_color=MUTED)
        self.status.pack(anchor="w", padx=20, pady=(0, 14))

    def _build_footer(self):
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=26, pady=(0, 14))
        footer.grid_columnconfigure(0, weight=1)
        self.hotkey_hint = ctk.CTkLabel(
            footer, text="🎯 全局热键：未启用", font=FONT_SMALL, text_color=MUTED,
        )
        self.hotkey_hint.pack(side="left")
        ctk.CTkLabel(
            footer, text=f"{APP_NAME} v3.0 · 全自动游戏喊话", font=FONT_SMALL, text_color="#4a5568",
        ).pack(side="right")

    # ---------------------------------------------------------------- 词库
    def _load_libraries(self, keep_selection=True):
        previous = self.library_combo.get()
        libs: dict[str, dict] = {}

        # 内置词库：data/*.txt
        if DATA_DIR.is_dir():
            for file in sorted(DATA_DIR.glob("*.txt")):
                items = self._read_txt(file)
                if items:
                    libs[file.stem] = {"items": items, "builtin": True}

        # 自定义词库：libraries/*.txt
        if LIB_DIR.is_dir():
            for file in sorted(LIB_DIR.glob("*.txt")):
                items = self._read_txt(file)
                if items:
                    libs[file.stem] = {"items": items, "builtin": False}

        self.libraries = libs
        names = list(libs.keys())
        self.library_combo.configure(values=names)
        if names:
            if keep_selection and previous in names:
                self.library_combo.set(previous)
            else:
                self.library_combo.set(names[0])
        else:
            self.library_combo.set("")
        self._update_count()
        total = sum(len(v["items"]) for v in libs.values())
        self._log(f"[系统] 词库加载完成：{len(libs)} 个词库 / 共 {total} 句")

    @staticmethod
    def _read_txt(file: Path) -> list[str]:
        for encoding in ("utf-8-sig", "gbk"):
            try:
                lines = file.read_text(encoding=encoding).splitlines()
                break
            except UnicodeDecodeError:
                continue
        else:
            return []
        items = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith(("#", "//")):
                items.append(line)
        return items

    def _reload_libraries(self):
        self._load_libraries(keep_selection=True)
        self._set_status("词库已刷新")

    def _current_library(self) -> str:
        return self.library_combo.get()

    def _current_items(self) -> list[str]:
        name = self._current_library()
        if name and name in self.libraries:
            return self.libraries[name]["items"]
        return []

    def _update_count(self):
        name = self._current_library()
        if name and name in self.libraries:
            info = self.libraries[name]
            tag = "内置" if info["builtin"] else "自定义"
            self.lib_info.configure(text=f"{name}（{tag}）· {len(info['items'])} 句")
        else:
            self.lib_info.configure(text="未选择词库")

    def _pick(self) -> str:
        pool = self._current_items()
        return random.choice(pool) if pool else ""

    def reroll(self):
        if not self._current_items():
            messagebox.showwarning("未选择词库", "请先选择一个词库。")
            return
        self.current_phrase = self._pick()
        self.preview_label.configure(text=self.current_phrase)
        self._set_status("已随机抽取，可点「发送这条」或按热键")

    def copy_current(self):
        if not self.current_phrase:
            messagebox.showwarning("无内容", "请先点「换一条」抽取语录。")
            return
        if keysim.set_clipboard(self.current_phrase):
            self._set_status("已复制到剪贴板", SUCCESS)
        else:
            self._set_status("复制失败：剪贴板被占用", DANGER)

    # ---------------------------------------------------------------- 发送
    def _flow_params(self) -> dict:
        mode = self.flow_mode.get()
        paste_mods = PASTE_COMBOS.get(self.paste_combo.get(), ("Ctrl",))
        chat_key = self.chat_key.get()
        try:
            chat_delay = max(0, min(5000, int(self.chat_delay.get())))
            enter_delay = max(0, min(5000, int(self.enter_delay.get())))
            loop_interval = max(300, min(60000, int(self.loop_interval.get())))
        except ValueError:
            raise ValueError("延迟参数必须是数字（毫秒）")
        return {
            "mode": mode, "paste_mods": paste_mods, "chat_key": chat_key,
            "chat_delay": chat_delay, "enter_delay": enter_delay,
            "loop_interval": loop_interval,
        }

    def _execute_flow(self, phrase: str, cfg: dict) -> None:
        """执行完整发送流程：开聊天框 → 剪贴板 → 粘贴 → 按需回车。
        cfg 由主线程传入（快照），后台线程不读 UI。"""
        if cfg["chat_key"] != "无":
            keysim.tap_key(cfg["chat_key"])
            time.sleep(cfg["chat_delay"] / 1000)
        if not keysim.set_clipboard(phrase):
            raise RuntimeError("剪贴板写入失败（可能被其他程序占用）")
        keysim.paste_with(cfg["paste_mods"])
        if cfg["mode"] == "粘贴并回车":
            time.sleep(cfg["enter_delay"] / 1000)
            keysim.press_enter()
        self.hotkey.suppress(0.6)  # 防止注入按键误触发热键

    def _run_send(self, phrase: str, by: str):
        try:
            cfg = self._flow_params()
        except ValueError as exc:
            self._set_status(f"{exc}", DANGER)
            return
        self.sending = True
        self._set_status(f"正在发送（{by}）…", ACCENT)
        self._log(f"[{by}] {phrase}", "#7ff5d8")

        def worker():
            try:
                self._execute_flow(phrase, cfg)
            except Exception as exc:
                self._ui(lambda: (
                    self._log(f"[错误] 发送失败：{exc}", "#ff9e9e"),
                    self._set_status(f"发送失败：{exc}", DANGER),
                    self._finish_send(),
                ))
            else:
                self._ui(lambda: (
                    self._log("[完成] 已全自动送达", SUCCESS),
                    self._set_status("发送完成", SUCCESS),
                    self._finish_send(),
                ))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_send(self):
        self.sending = False

    # ---------------------------------------------------------------- 循环
    def _toggle_loop(self):
        if self.looping:
            self._stop_loop()
        else:
            self._start_loop()

    def _start_loop(self):
        if self.looping:
            return
        if not self._current_items():
            messagebox.showwarning("未选择词库", "请先选择一个词库。")
            return
        try:
            cfg = self._flow_params()
        except ValueError as exc:
            self._set_status(f"{exc}", DANGER)
            return
        # 快照：后台线程只读快照，不碰 UI（循环期间改词库/参数不影响本次循环）
        self._loop_cfg = cfg
        self._loop_pool = self._current_items()
        self.looping = True
        self.stop_event.clear()
        self.loop_btn.configure(text="⏹ 停止循环")
        self._set_status("循环发送中…（再按热键停止）", ACCENT)
        self._log("[循环] 已开启 —— 按热键或点「停止循环」关闭")
        self._play_beep("start")
        threading.Thread(target=self._loop_worker, daemon=True).start()

    def _stop_loop(self):
        if not self.looping:
            return
        self.stop_event.set()
        self._log("[循环] 已请求停止，等待当前一句发送完成…")
        self._set_status("正在停止循环…", WARN)
        self._play_beep("stop")

    def _loop_worker(self):
        pool = self._loop_pool
        cfg = self._loop_cfg
        interval = cfg["loop_interval"] / 1000
        while not self.stop_event.is_set():
            if not pool:
                self._ui(lambda: self._set_status("词库为空，循环已停止", DANGER))
                break
            phrase = random.choice(pool)
            self._ui(lambda p=phrase: (
                self.preview_label.configure(text=p),
                self._log(f"[循环] {p}", "#7ff5d8"),
            ))
            try:
                self._execute_flow(phrase, cfg)
            except Exception as exc:
                self._ui(lambda e=exc: self._log(f"[循环] 发送失败：{e}", "#ff9e9e"))
            if self.stop_event.wait(interval):
                break
        self._ui(self._finish_loop)

    def _finish_loop(self):
        self.looping = False
        self.loop_btn.configure(text="🔁 开始循环")
        self._set_status("循环已停止", WARN)
        self._log("[循环] 已停止")

    def _play_beep(self, kind: str):
        """开关提示音：开启为上行双音，关闭为下行双音（异步播放，不卡界面）。"""
        def run():
            try:
                if kind == "start":
                    winsound.Beep(880, 110)
                    time.sleep(0.06)
                    winsound.Beep(1318, 110)
                else:
                    winsound.Beep(660, 110)
                    time.sleep(0.06)
                    winsound.Beep(440, 170)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def send_current(self):
        if self.sending:
            self._set_status("正在发送中，请稍候…", WARN)
            return
        if not self.current_phrase:
            if not self._current_items():
                messagebox.showwarning("未选择词库", "请先选择一个词库。")
                return
            self.current_phrase = self._pick()
            self.preview_label.configure(text=self.current_phrase)
        self._run_send(self.current_phrase, by="手动")

    # ---------------------------------------------------------------- 热键
    def _hotkey_label(self) -> str:
        mods = "+".join(
            name for name, var in (("Ctrl", self.mod_ctrl), ("Alt", self.mod_alt), ("Shift", self.mod_shift)) if var.get()
        )
        return f"{mods}+{self.hotkey_key.get()}" if mods else self.hotkey_key.get()

    def _hotkey_mods(self) -> tuple[str, ...]:
        return tuple(
            name for name, var in (("Ctrl", self.mod_ctrl), ("Alt", self.mod_alt), ("Shift", self.mod_shift)) if var.get()
        )

    def _apply_hotkey(self):
        label = self._hotkey_label()
        if self.hotkey_enabled.get():
            if self.hotkey.start(self.hotkey_key.get(), self._hotkey_mods()):
                self.hotkey_hint.configure(
                    text=f"🎯 全局热键：{label} 已启用 —— 游戏中按 {label} 开始/停止循环喊话", text_color=ACCENT)
                self.header_badge.configure(text=f"● 热键 {label}", text_color=ACCENT)
            else:
                self.hotkey_enabled.set(False)
                self.hotkey_hint.configure(text="🎯 全局热键：无效按键", text_color=DANGER)
                self.header_badge.configure(text="● 热键无效", text_color=DANGER)
        else:
            self.hotkey.stop()
            self.hotkey_hint.configure(text="🎯 全局热键：未启用", text_color=MUTED)
            self.header_badge.configure(text="● 热键未启用", text_color=MUTED)

    def _hotkey_pressed(self):
        """监听线程回调 → 切回主线程处理。"""
        self._ui(self._on_hotkey)

    def _on_hotkey(self):
        if not self.hotkey_enabled.get():
            return
        if self.sending:
            self._set_status("正在发送中，本次热键忽略", WARN)
            return
        if self.looping:
            self._stop_loop()
        else:
            self._start_loop()

    # ---------------------------------------------------------------- 日志与配置
    def _log(self, line: str, color=None):
        stamp = f"[{datetime.now():%H:%M:%S}] "
        self.log.insert("end", stamp + line + "\n")
        self.log.see("end")

    def _set_status(self, text, color=MUTED):
        self.status.configure(text=text, text_color=color)

    def save_config(self):
        data = {
            "hotkey": {
                "enabled": bool(self.hotkey_enabled.get()),
                "key": self.hotkey_key.get(),
                "mods": list(self._hotkey_mods()),
            },
            "flow": {
                "chat_key": self.chat_key.get(),
                "chat_delay": self.chat_delay.get(),
                "mode": self.flow_mode.get(),
                "paste": self.paste_combo.get(),
                "enter_delay": self.enter_delay.get(),
                "loop_interval": self.loop_interval.get(),
            },
            "library": self._current_library(),
        }
        try:
            CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._set_status(f"保存失败：{exc}", DANGER)
            return
        self._set_status("配置已保存", SUCCESS)

    def _load_config(self):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        hotkey_cfg = data.get("hotkey", {})
        if hotkey_cfg.get("key"):
            self.hotkey_key.set(str(hotkey_cfg["key"]))
        mods = set(hotkey_cfg.get("mods", []))
        self.mod_ctrl.set("Ctrl" in mods)
        self.mod_alt.set("Alt" in mods)
        self.mod_shift.set("Shift" in mods)
        if hotkey_cfg.get("enabled"):
            self.hotkey_enabled.set(True)
            self._apply_hotkey()
        flow = data.get("flow", {})
        if flow.get("chat_key") in CHAT_KEYS:
            self.chat_key.set(flow["chat_key"])
        if flow.get("mode") in FLOW_MODES:
            self.flow_mode.set(flow["mode"])
        if flow.get("paste") in PASTE_COMBOS:
            self.paste_combo.set(flow["paste"])
        for key, var in (("chat_delay", self.chat_delay), ("enter_delay", self.enter_delay), ("loop_interval", self.loop_interval)):
            if flow.get(key):
                var.set(str(flow[key]))
        if data.get("library") in self.libraries:
            self.library_combo.set(data["library"])
        self._update_count()

    def _on_close(self):
        self.stop_event.set()
        self.hotkey.stop()
        self.save_config()
        self.destroy()


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    app = GameSayApp()
    app.mainloop()
