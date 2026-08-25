"""全局热键监听模块（纯 ctypes，无第三方依赖）。

通过轮询 GetAsyncKeyState 检测按键上升沿，支持 Ctrl / Alt / Shift 组合键，
可以在游戏全屏/前台运行时也能收到按键事件（Windows 全局生效，无需窗口焦点）。

本模块还支持「抑制窗口」：程序自己模拟按键后调用 suppress()，
避免注入的按键（如 Ctrl+V）被误判为热键再次触发。
"""

import ctypes
import threading
import time

user32 = ctypes.windll.user32
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

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


def is_key_down(vk: int) -> bool:
    """当前该键是否处于按下状态。"""
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class HotkeyListener:
    """全局热键监听器：后台线程轮询按键状态，检测到上升沿时回调。

    注意：on_press 在监听线程中调用，若需要更新 UI 请自行切换到主线程
    （例如通过队列），本类不关心线程模型。
    """

    def __init__(self, on_press, poll_interval: float = 0.05):
        self.on_press = on_press
        self.poll = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._key: int | None = None
        self._mods: tuple[int, ...] = ()
        self._prev_down = False
        self._suppress_until = 0.0

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
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="hotkey-listener"
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=1.0)
            self._thread = None

    def suppress(self, seconds: float = 0.6) -> None:
        """在未来 seconds 秒内忽略所有按键（用于程序自动模拟按键后）。"""
        self._suppress_until = time.monotonic() + seconds

    def _run(self) -> None:
        while not self._stop.is_set():
            if time.monotonic() >= self._suppress_until:
                main_down = is_key_down(self._key)
                mods_ok = all(is_key_down(vk) for vk in self._mods)
                pressed = main_down and mods_ok
                if pressed and not self._prev_down:  # 上升沿
                    try:
                        self.on_press()
                    except Exception:
                        pass
                self._prev_down = pressed
            self._stop.wait(self.poll)
