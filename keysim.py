"""按键模拟 + 剪贴板写入（纯 ctypes，无第三方依赖）。

- set_clipboard：以 UTF-16 写入剪贴板（对中文安全）
- paste_with：模拟「修饰键 + V」粘贴（V 用扫描码发送，兼容 DirectInput 游戏）
- press_enter：模拟回车

所有按键通过 SendInput 注入系统输入队列，发送给当前前台窗口，
因此游戏全屏/前台时也能生效。
"""

import ctypes
import time
from ctypes import wintypes

from hotkey import VK_MAP

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_RETURN = 0x0D

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040
CF_UNICODETEXT = 13

# ---- 声明 Win32 函数签名（64 位下句柄必须用指针宽度，否则会被截断） ----
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR，必须是 8 字节
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT(ctypes.Structure):
    """必须与 Windows 的 INPUT 完全一致（x64 下 sizeof = 40），
    否则 SendInput 返回 0 / ERROR_INVALID_PARAMETER。"""
    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


assert ctypes.sizeof(KEYBDINPUT) == 24, ctypes.sizeof(KEYBDINPUT)
assert ctypes.sizeof(INPUT) == 40, ctypes.sizeof(INPUT)


def _make_input(vk=0, scan=0, flags=0) -> INPUT:
    ki = KEYBDINPUT()
    ki.wVk = vk
    ki.wScan = scan
    ki.dwFlags = flags
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = ki
    return inp


def _send(inputs) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(arr), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise RuntimeError(
            f"SendInput 注入失败（{sent}/{len(inputs)}，错误码 {kernel32.GetLastError()}）"
        )
    return sent


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _tap(vk=0, scan=0, scancode=False, gap: float = 0.035) -> None:
    """按下-释放一个键。scancode=True 时用扫描码发送（对游戏兼容性更好）。"""
    flags = KEYEVENTF_SCANCODE if scancode else 0
    _send((_make_input(vk, scan, flags),))
    time.sleep(gap)
    _send((_make_input(vk, scan, flags | KEYEVENTF_KEYUP),))


def set_clipboard(text: str, retries: int = 15) -> bool:
    """写入剪贴板（UTF-16，中文安全）。返回是否成功。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                data = text.encode("utf-16-le") + b"\x00\x00"
                handle = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(data))
                pointer = kernel32.GlobalLock(handle)
                ctypes.memmove(pointer, data, len(data))
                kernel32.GlobalUnlock(handle)
                user32.SetClipboardData(CF_UNICODETEXT, handle)
                return True
            except Exception:
                return False
            finally:
                user32.CloseClipboard()
        time.sleep(0.05)
    return False


def paste_with(mods: tuple[str, ...] = ("Ctrl",)) -> None:
    """模拟「修饰键 + V」粘贴。V 用扫描码，修饰键用虚拟键码。"""
    vk_v = ord("V")
    scan_v = user32.MapVirtualKeyW(vk_v, 0)  # MAPVK_VK_TO_VSC
    mod_vks = [VK_MAP[m] for m in mods if m in VK_MAP]
    for vk in mod_vks:  # 修饰键按下
        _send((_make_input(vk, 0, 0),))
    time.sleep(0.03)
    _tap(scan=scan_v, scancode=True)
    time.sleep(0.03)
    for vk in reversed(mod_vks):  # 修饰键释放
        _send((_make_input(vk, 0, KEYEVENTF_KEYUP),))


def press_enter(gap: float = 0.035) -> None:
    """模拟回车。"""
    _tap(vk=VK_RETURN)


def tap_key(key_name: str) -> None:
    """模拟按下一个命名按键（用于「打开聊天框」等自定义键）。

    字母/数字用扫描码发送（兼容 DirectInput 游戏），特殊键用虚拟键码。
    key_name 取 hotkey.VK_MAP 中的名字，如 "回车"、"T"、"Y"、"/"、"F1"。
    """
    vk = VK_MAP.get(key_name)
    if vk is None:
        raise ValueError(f"未知按键：{key_name}")
    if key_name in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        scan = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        _tap(scan=scan, scancode=True)
    else:
        _tap(vk=vk)
