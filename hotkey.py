"""全局热键监听模块（纯 ctypes，无第三方依赖）。

双通道检测，取长补短：
1. **低级键盘钩子 WH_KEYBOARD_LL** —— DirectInput 独占键盘的游戏也收得到；
2. **GetAsyncKeyState 轮询** —— 管理员权限的游戏进程按键也能读到
   （钩子受 UIPI 限制看不到高权限进程的输入，轮询不受影响）。

两通道命中后按 0.3s 时间窗去重，避免重复触发。支持 Ctrl / Alt / Shift
组合键与抑制窗口（程序自动模拟按键后 suppress()，防止注入键误触发热键）。
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]
user32.CallNextHookEx.restype = ctypes.c_long
user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.restype = ctypes.c_long
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

# 按键名 -> 虚拟键码
VK_MAP: dict[str, int] = {}
for i in range(1, 13):
    VK_MAP[f"F{i}"] = 0x70 + i - 1
for i in range(10):
    VK_MAP[str(i)] = 0x30 + i
for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    VK_MAP[ch] = ord(ch)
VK_MAP["空格"] = 0x20
VK_MAP["回车"] = 0x0D
VK_MAP["Ctrl"] = 0x11
VK_MAP["Alt"] = 0x12
VK_MAP["Shift"] = 0x10
# 游戏中常见的打开聊天框按键
VK_MAP["~"] = 0xC0   # VK_OEM_3
VK_MAP["/"] = 0xBF   # VK_OEM_2

# 界面下拉框可选项（主键）
KEY_CHOICES: list[str] = (
    [f"F{i}" for i in range(1, 13)]
    + [str(i) for i in range(10)]
    + list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + ["空格"]
)

# 修饰键
MODIFIER_CHOICES: tuple[str, ...] = ("Ctrl", "Alt", "Shift")

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


KBDLLHOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p)


def is_key_down(vk: int) -> bool:
    """当前该键是否处于按下状态（异步键盘状态，全局有效）。"""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyListener:
    """全局热键监听器（钩子 + 轮询双通道）。

    注意：on_press 可能在钩子消息泵线程或轮询线程中调用，若需要更新 UI
    请自行切换到主线程（例如通过队列），本类不关心线程模型。
    """

    def __init__(self, on_press, poll_interval: float = 0.05):
        self.on_press = on_press
        self.poll = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None      # 轮询线程
        self._pump_thread: threading.Thread | None = None  # 钩子消息泵线程
        self._hook: int | None = None
        self._hook_cb = None
        self._key: int | None = None
        self._mods: tuple[int, ...] = ()
        self._prev_down = False
        self._suppress_until = 0.0
        self._last_fire = 0.0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, key_name: str, modifiers: tuple[str, ...] = ()) -> bool:
        """绑定并启动监听。modifiers 取 MODIFIER_CHOICES 中的名字。"""
        self.stop()
        vk = VK_MAP.get(key_name)
        if vk is None:
            return False
        self._key = vk
        self._mods = tuple(VK_MAP[m] for m in modifiers if m in MODIFIER_CHOICES)
        self._stop.clear()
        self._prev_down = False
        self._suppress_until = 0.0
        self._last_fire = 0.0
        self._install_hook()
        self._thread = threading.Thread(
            target=self._run_poll, daemon=True, name="hotkey-poll"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._hook:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
            self._hook = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
            self._pump_thread = None

    def suppress(self, seconds: float = 0.6) -> None:
        """在未来 seconds 秒内忽略所有按键（用于程序自动模拟按键后）。"""
        self._suppress_until = time.monotonic() + seconds

    # ------------------------------------------------------------ 钩子通道
    def _install_hook(self) -> None:
        try:
            self._hook_cb = KBDLLHOOKPROC(self._hook_handler)
            hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._hook_cb, kernel32.GetModuleHandleW(None), 0
            )
            if hook:
                self._hook = hook
                self._pump_thread = threading.Thread(
                    target=self._msg_pump, daemon=True, name="hotkey-hook-pump"
                )
                self._pump_thread.start()
        except Exception:
            self._hook = None

    def _msg_pump(self) -> None:
        msg = wintypes.MSG()
        while not self._stop.is_set():
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _hook_handler(self, nCode: int, wParam: int, lParam) -> int:
        if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            try:
                kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self._key and kbd.vkCode == self._key and all(
                    is_key_down(vk) for vk in self._mods
                ):
                    self._fire()
            except Exception:
                pass
        try:
            return user32.CallNextHookEx(self._hook, nCode, wParam, lParam)
        except Exception:
            return 0

    # ------------------------------------------------------------ 轮询通道
    def _run_poll(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() >= self._suppress_until:
                main_down = is_key_down(self._key)
                mods_ok = all(is_key_down(vk) for vk in self._mods)
                pressed = main_down and mods_ok
                if pressed and not self._prev_down:  # 上升沿
                    self._fire()
                self._prev_down = pressed
            self._stop.wait(self.poll)

    # ------------------------------------------------------------ 统一触发
    def _fire(self) -> None:
        now = time.monotonic()
        if now < self._suppress_until:
            return
        if now - self._last_fire < 0.3:  # 双通道去重
            return
        self._last_fire = now
        try:
            self.on_press()
        except Exception:
            pass
