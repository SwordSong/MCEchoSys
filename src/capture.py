"""
capture.py — 游戏截屏引擎模块
核心功能：根据已知的游戏主进程名自动查找到鸣潮窗口，并源源不断地从后台获取这块窗口的绘制缓冲区。
工作原理：通常优先利用 Windows 自带的一些高效截屏 API (如 BitBlt，或是如果支持则用 WGC)。
为了防止被游戏窗口置于后台导致的黑屏/停止渲染影响，内部做了一定程度的兼容性保底支持操作。
输出统一为 Python 标准的 Numpy / OpenCV 图像矩阵 (BGR 格式)，以供后续 YOLO 等 AI 工具直读分析。
"""
import ctypes
import ctypes.wintypes as wintypes
import time
import datetime
import threading
import os
import logging
import subprocess
from typing import Optional, List, Tuple

import psutil
import mss
import numpy as np

# Performance Monitor Switch
ENABLE_PERF_STATS = os.getenv("MC_LOG_PERF", "1") == "1"
 
# Global performance state
_g_perf_lock = threading.Lock()
_g_perf_stats = {
    "cpu": 0.0,
    "mem_mb": 0.0,
    "gpu_util": -1, # -1 means unknown/unavailable
}

def _perf_monitor_loop():
    """Background thread to update performance metrics periodically."""
    proc = psutil.Process()
    # Pre-warm cpu_percent
    proc.cpu_percent()
    
    while True:
        try:
            # 1. CPU & Memory (Process specific)
            cpu = proc.cpu_percent()
            mem = proc.memory_info().rss / (1024 * 1024)
            
            # 2. GPU (System wide via nvidia-smi, if available)
            gpu = -1
            try:
                # Use subprocess to query nvidia-smi with minimal overhead
                # preventing console window flash on Windows
                startupinfo = None
                if os.name == 'nt':
                     startupinfo = subprocess.STARTUPINFO()
                     startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                
                output = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                    encoding="utf-8",
                    startupinfo=startupinfo,
                    timeout=1.0
                )
                if output:
                    # Take the first GPU if multiple
                    val = output.strip().split('\n')[0]
                    gpu = int(val)
            except Exception:
                pass
                
            with _g_perf_lock:
                _g_perf_stats["cpu"] = cpu
                _g_perf_stats["mem_mb"] = mem
                _g_perf_stats["gpu_util"] = gpu
                
        except Exception:
            pass
        
        # Update interval: 1.5s (balance between freshness and overhead)
        time.sleep(1.5)

# Start monitor thread if enabled
if ENABLE_PERF_STATS:
    _t = threading.Thread(target=_perf_monitor_loop, daemon=True, name="PerfMonitor")
    _t.start()
try:
    import win32gui
    import win32ui
    import win32con
    import win32api
    HAS_PYWIN32 = True
except Exception:
    HAS_PYWIN32 = False
try:
    import cv2
except Exception:
    cv2 = None

# Optional: try to import dxcam for DirectX capture
try:
    import dxcam
    HAS_DXCAM = True
except ImportError:
    HAS_DXCAM = False

# Force-disable DXGI/dxcam usage: this module will use PrintWindow/BitBlt
# exclusively for window capture as requested.
# HAS_DXCAM = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 尝试尽早启用 DPI 感知，使窗口客户区坐标映射到物理像素（
# 避免在 Windows 显示缩放开启时出现截取被裁剪的情况）。尽力尝试
# 新的 API，失败时回退到兼容方案。
try:
    try:
        _shcore = ctypes.windll.shcore
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        _shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            _user32 = ctypes.windll.user32
            # Prefer SetProcessDpiAwarenessContext if available (Windows 10+)
            if hasattr(_user32, 'SetProcessDpiAwarenessContext'):
                # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
                try:
                    _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
                except Exception:
                    # fallback to SetProcessDPIAware
                    try:
                        _user32.SetProcessDPIAware()
                    except Exception:
                        pass
            else:
                try:
                    _user32.SetProcessDPIAware()
                except Exception:
                    pass
        except Exception:
            pass
except Exception:
    pass

# 安装一个轻量级的打印过滤器，用于检测并抑制意外打印全局 `sys.path` 列表。
# 一些第三方调试代码会重复打印解释器路径，导致控制台被淹没；这里包裹
# `print`，当检测到打印内容正好是 `sys.path` 时，抑制完整输出并打印一行简短
# 的来源位置以便定位。
try:
    import builtins, sys, traceback
    _orig_print = builtins.print

    def _filtered_print(*args, **kwargs):
        try:
            if len(args) == 1:
                a = args[0]
                # Compare by repr to catch equivalent list copies from other modules
                if isinstance(a, (list, tuple)) and repr(a) == repr(sys.path):
                    # record a short caller location then suppress full path print
                    stack = traceback.extract_stack(limit=4)
                    caller = stack[-3] if len(stack) >= 3 else None
                    loc = f"{caller.filename}:{caller.lineno}" if caller is not None else "<unknown>"
                    _orig_print(f"[捕获] 已抑制 sys.path 打印，来源：{loc}")
                    return
            # 同步到日志，以便 UI 捕获
            try: 
                msg = " ".join(str(x) for x in args)
                logger.info(msg)
            except Exception:
                pass
        except Exception:
            pass
            
        # Add timestamp prefix and perf suffix for raw print output
        try:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            new_args = list(args) if args else []

            # Add Perf Stats Suffix
            if ENABLE_PERF_STATS:
                # Read stats with lock
                cpu_val = 0.0
                mem_val = 0.0
                gpu_val = -1
                
                # Check global variable existence (defined above)
                if '_g_perf_stats' in globals():
                    # We might not import _g_perf_lock since it was inside the previous block's scope?
                    # No, it was global. But we need to use 'global' keyword if updating, reading is fine.
                    # But wait, globals are module level.
                    try:
                        cpu_val = _g_perf_stats.get("cpu", 0.0)
                        mem_val = _g_perf_stats.get("mem_mb", 0.0)
                        gpu_val = _g_perf_stats.get("gpu_util", -1)
                    except Exception:
                        pass
                
                gpu_str = f"{gpu_val}%" if gpu_val >= 0 else "N/A"
                # Format: [CPU:12.3% Mem:450MB GPU:45%]
                # stats_str = f"[CPU:{cpu_val:.1f}% Mem:{mem_val:.0f}MB GPU:{gpu_str}]"
                # new_args.append(stats_str)

            if new_args and len(new_args) > (1 if ENABLE_PERF_STATS else 0):
                 # Prepend timestamp to the first argument
                 new_args[0] = f"[{ts}] {new_args[0]}"
            else:
                 # If empty args (print()) or only stats, insert timestamp at head
                 new_args.insert(0, f"[{ts}]")
            
            return _orig_print(*new_args, **kwargs)
        except Exception:
            return _orig_print(*args, **kwargs)

    builtins.print = _filtered_print
except Exception:
    # best-effort; if wrapper fails, don't block capture functionality
    pass


def _create_dxcam_safe(device_idx: int = 0):
    """
    备用截屏技术 1 (DXCam) - 安全工厂构建器：
    利用 DXGI 桌面复制方式的高速截取方案作为截取引擎时，可能在结束时由于摄像头没对齐出现属性错误，
    这里显式加上安全检查，避免主解释器退出时出现恶心红字 AttributeError 抛错。创建失败返回 None。
    """
    if not HAS_DXCAM:
        return None

    # 尝试多种创建策略并报告失败以便调试。
    last_exc = None
    create_fn = getattr(dxcam, "create", None)
    device_candidates = [device_idx, 0, 1, 2]
    for d in device_candidates:
        if d is None:
            continue
        try:
            if callable(create_fn):
                cam = create_fn(device_idx=d)
            else:
                # fallback: try calling dxcam() if API differs
                cam = dxcam(d)
            # 确保实例具有后续代码使用到的最小属性
            try:
                if not hasattr(cam, "is_capturing"):
                    setattr(cam, "is_capturing", False)
            except Exception:
                pass
            # 如果存在 start() 方法则尝试调用以预热摄像头
            try:
                if hasattr(cam, "start"):
                    try:
                        cam.start()
                    except Exception:
                        # non-fatal: just continue
                        pass
            except Exception:
                pass
            return cam
        except Exception as e:
            last_exc = e
            try:
                print(f"[dxcam] 创建失败 device_idx={d}: {e}")
            except Exception:
                pass
    try:
        if last_exc is not None:
            print(f"[dxcam] 所有创建尝试失败，最近错误：{last_exc}")
    except Exception:
        pass
    return None
    
# For some dxcam versions the DXCamera class lacks an `is_capturing` attribute
# which its __del__/stop implementation expects; set a class-level default
# to avoid AttributeError during interpreter shutdown for instances we did
# not create or could not monkeypatch individually.
try:
    if HAS_DXCAM and hasattr(dxcam, "DXCamera"):
        if not hasattr(dxcam.DXCamera, "is_capturing"):
            dxcam.DXCamera.is_capturing = False
except Exception:
    pass
try:
    # Override problematic DXCamera.__del__ with a safe variant that swallows
    # errors during interpreter shutdown to prevent noisy AttributeError
    if HAS_DXCAM and hasattr(dxcam, "DXCamera"):
        def _dxcam_safe_del(self):
            try:
                rel = getattr(self, "release", None)
                if callable(rel):
                    try:
                        rel()
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            dxcam.DXCamera.__del__ = _dxcam_safe_del
        except Exception:
            pass
except Exception:
    pass

# ── WGC (Windows Graphics Capture) ──────────────────────────────
HAS_WGC = False
try:
    # 尝试导入 windows_capture 库 (第三方库，pip install windows-capture)
    # 这个库提供了对 WGC 的 Python 封装
    from windows_capture import WindowsCapture, Frame, InternalCaptureControl
    HAS_WGC = True
except ImportError:
    HAS_WGC = False


class WGCBackend:
    """封装 Windows Graphics Capture (WGC) 的后端类，使用 windows_capture 库。"""
    
    def __init__(self, fps=30):
        self.fps = fps
        self.lock = threading.Lock()
        self.last_frame = None
        self.capture_control = None
        self.current_window_name = None
        self.active = False
        self._last_capture_time = 0.0
        self._capture_interval = 1.0 / (fps if fps > 0 else 30)
        
    def _on_frame_arrived(self, frame: 'Frame', capture_control: 'InternalCaptureControl'):
        # 限制捕获帧率以节省 CPU/带宽资源，无需处理每一次屏幕刷新
        # 高刷新率下 WGC 回调非常密集，尽管 Python 的 Global Interpreter Lock (GIL) 
        # 会限制多线程并行，但密集回调仍会挤占主线程 CPU 时间片。
        now = time.time()
        # 使用严格的 >= 检查，避免微小计时误差导致的漏帧或过快
        if now - self._last_capture_time < self._capture_interval:
            return
        self._last_capture_time = now

        # frame.frame_buffer 是 BGRA (或类似), 需要转换
        # windows_capture 的 Frame 对象有 width, height, frame_buffer (numpy array)
        try:
            # 原始 buffer 可能是 BGRA
            # 重要：此处发生内存拷贝。如果 fps 很高，这一步是性能杀手。
            # 由于上面已经限流，只有通过限流的帧才会执行此操作。
            arr = frame.frame_buffer
            
            # 移除 Alpha 通道 BGRA -> BGR
            # 注意: 如果不需要 alpha, 可以直接切片
            if arr is not None:
                # 检查是否为空或全黑 (可选)
                pass
                
                # 再次优化：只在确实需要时才拷贝转换
                # 使用 np.ascontiguousarray 确保内存连续，提高后续处理速度
                # 切片操作 arr[:, :, :3] 会创建视图，但 copy() 会触发实际数据拷贝
                
                # 预先处理好数据再加锁，减小锁粒度（虽然 Python GIL 也是个大锁）
                # 这里只保留必要的拷贝
                # final_frame = arr[:, :, :3].copy()
                
                # 终极优化：直接使用视图 (view)，不做全图深拷贝，只有在 UI 线程取用时让 UI 线程去处理
                # 但考虑到可能会有内存安全问题（底层buffer释放），必须至少持有引用
                
                # 妥协方案：只在 capture 线程内做一次从 BGRA 到 BGR 的拷贝
                # 避免在回调中进行任何多余的 Python 对象创建，使用切片+copy是最快的
                if self.lock.acquire(blocking=False):
                    try:
                        self.last_frame = arr[:, :, :3].copy()
                    finally:
                        self.lock.release()
                else:
                    # 如果锁被主循环持有（正在读取），则丢弃这一帧，绝不阻塞 WGC 线程
                    pass
        except Exception:
            pass

    def _on_closed(self):
        with self.lock:
            self.active = False
            # print("WGC Session Closed")

    def start(self, hwnd: int) -> bool:
        if not HAS_WGC:
            return False
            
        # windows_capture 目前主要支持通过窗口标题捕获
        # 我们需要获取窗口标题
        title = get_window_title(hwnd)
        if not title:
            return False
            
        # 如果已经有活动的捕获且标题没变，则无需重启
        if self.active and self.current_window_name == title:
            return True
            
        # 停止旧的
        self.stop()
        
        try:
            print(f"[WGC] 尝试捕获窗口: '{title}' (hwnd={hwnd})")
            # 创建 WindowsCapture 实例
            # 注意: windows_capture 库通过 window_name 来查找窗口
            self.capture = WindowsCapture(
                cursor_capture=False,
                draw_border=False,
                window_name=title.strip()  # Remove trailing spaces
            )
            
            # 注册回调
            @self.capture.event
            def on_frame_arrived(frame, capture_control):
                self._on_frame_arrived(frame, capture_control)
                
            @self.capture.event
            def on_closed():
                self._on_closed()
                
            # 启动线程
            self.capture_control = self.capture.start_free_threaded()
            self.current_window_name = title
            self.active = True
            
            # 等待第一帧? 不强求，异步更新
            return True
        except Exception as e:
            print(f"[WGC] 启动失败 (title={title!r}): {e}")
            self.active = False
            return False

    def stop(self):
        # 停止捕获控制
        if self.capture_control:
            try:
                # 尝试调用 stop
                if hasattr(self.capture_control, "stop"):
                    self.capture_control.stop()
            except Exception:
                pass
            self.capture_control = None
            
        self.capture = None
        self.active = False
        self.current_window_name = None

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self.lock:
             return self.last_frame



# Since complete WGC implementation is complex to inline, we use a placeholder that
# warns the user or falls back if dependencies aren't perfect.
# But we changed the code structure to support it.

# Now we modify CaptureWorker to use WGC if requested.

class DXCamBackend:
    def __init__(self, fps=30):
        self.camera = None
        self.active = False
        self.fps = fps
    
    def start(self, hwnd=None):
        if not HAS_DXCAM:
            return False
        if self.active and self.camera:
            return True
            
        try:
            # Create camera on default output (monitor 0)
            # The helper _create_dxcam_safe does detailed checks
            self.camera = _create_dxcam_safe(0)
            if self.camera:
                # We use manual polling (.grab()) to better control CPU usage
                # If the helper started the camera (threaded mode), stop it.
                if hasattr(self.camera, "is_capturing") and self.camera.is_capturing:
                    if hasattr(self.camera, "stop"):
                        self.camera.stop()
                
                self.active = True
                return True
        except Exception as e:
            print(f"[DXCam] Init failed: {e}")
            self.active = False
            
        return False

    def stop(self):
        if self.camera:
            try:
                if hasattr(self.camera, "stop"):
                    self.camera.stop()
                elif hasattr(self.camera, "release"):
                    self.camera.release()
            except Exception:
                pass
            self.camera = None
        self.active = False

    def get_latest_frame(self, region=None) -> Optional[np.ndarray]:
        if not self.active or not self.camera:
            return None
        try:
            # grab() returns the latest frame (blocking until next frame or timeout)
            # region=(left, top, right, bottom)
            return self.camera.grab(region=region)
        except Exception:
            return None


class CaptureWorker(threading.Thread):
    def __init__(self, proc_name: str = "Client-Win64-Shipping.exe", fps: int = 15, capture_mode: str = "wgc"):
        super().__init__(daemon=True)
        self.proc_name = proc_name
        self.fps = fps
        self.capture_mode = capture_mode
        self._stop_evt = threading.Event()
        self.last_frame: Optional[np.ndarray] = None
        self.hwnd: Optional[int] = None
        self._lock = threading.Lock()
        self._wgc_backend = None
        self._dxcam_backend = None
        self._frame_count = 0
        self._start_time = time.time()

    def stop(self):
        self._stop_evt.set()
        if self._wgc_backend:
            self._wgc_backend.stop()
        if self._dxcam_backend:
            self._dxcam_backend.stop()

    @property
    def frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self.last_frame

    @property
    def actual_fps(self) -> float:
        elapsed_time = time.time() - self._start_time
        if elapsed_time > 0:
            return self._frame_count / elapsed_time
        return 0.0

    def run(self):
        print(f"[捕获线程] 启动，模式={self.capture_mode}")
        self._start_time = time.time()
        self._frame_count = 0
        self._last_log_time = 0.0
        
        while not self._stop_evt.is_set():
            # 1. Ensure Window Handle
            if not self.hwnd or not _IsWindowVisible(self.hwnd):
                self.hwnd = find_game_hwnd(self.proc_name)
                if self.hwnd:
                    print(f"[捕获线程] 找到窗口: {self.hwnd}")
                    
                    # Try to initialize backends based on mode
                    # If mode is 'wgc', we try WGC -> DXCam -> PrintWindow
                    if self.capture_mode == "wgc":
                        # Try WGC first.
                        if HAS_WGC:
                            if not self._wgc_backend:
                                 self._wgc_backend = WGCBackend(self.fps)
                            if self._wgc_backend.start(self.hwnd):
                                print("[捕获线程] 已启用 WGC 截图后端")
                            else:
                                print("[捕获线程] WGC 启动失败，尝试 DXCam...")

                        # Fallback to DXCam if WGC is unavailable or failed.
                        is_wgc_ok = self._wgc_backend and self._wgc_backend.active
                        if not is_wgc_ok and HAS_DXCAM:
                            if not self._dxcam_backend:
                                self._dxcam_backend = DXCamBackend(self.fps)
                            if self._dxcam_backend.start(self.hwnd):
                                print("[捕获线程] WGC 不可用，回退到 DXCam (DXGI) 截图后端")
                            else:
                                print("[捕获线程] DXCam 启动失败")

            
            # 2. Capture Frame
            if self.hwnd:
                active_mode = "Unknown"
                frame = None

                # A. WGC
                if self.capture_mode == "wgc" and self._wgc_backend and self._wgc_backend.active:
                     active_mode = "WGC"
                     frame = self._wgc_backend.get_latest_frame()

                # B. DXCam
                elif self.capture_mode == "wgc" and self._dxcam_backend and self._dxcam_backend.active:
                     active_mode = "DXCam"
                     try:
                         # Get client rect in screen coordinates
                         l, t, r, b = get_client_rect(self.hwnd)
                         if r > l and b > t:
                             frame = self._dxcam_backend.get_latest_frame(region=(l, t, r, b))
                     except Exception:
                         pass
                
                # C. PrintWindow (Fallback)
                if frame is None:
                    active_mode = "PrintWindow"
                    frame = capture_window_printwindow(self.hwnd)
                
                # Store
                if frame is not None:
                    with self._lock:
                        self.last_frame = frame
                    self._frame_count += 1
                
                # Periodic Log
                now_t = time.time()
                if now_t - self._last_log_time > 3.0:
                    print(f"[捕获] 模式={active_mode} fps={self.actual_fps:.1f}")
                    self._last_log_time = now_t
            
            time.sleep(1.0 / self.fps)
        
        # Cleanup
        if self._wgc_backend:
            self._wgc_backend.stop()
        if self._dxcam_backend:
            self._dxcam_backend.stop()




# ── Win32 helpers ──────────────────────────────────────────────
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
_EnumWindows = user32.EnumWindows
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_GetWindowThreadProcessId = user32.GetWindowThreadProcessId
_IsWindowVisible = user32.IsWindowVisible
_GetWindowTextLengthW = user32.GetWindowTextLengthW
_GetWindowTextW = user32.GetWindowTextW
_GetClientRect = user32.GetClientRect
_ClientToScreen = user32.ClientToScreen
_GetWindowRect = user32.GetWindowRect
_GetDC = user32.GetDC
_ReleaseDC = user32.ReleaseDC

# `PrintWindow` 函数原型
try:
    _PrintWindow = user32.PrintWindow
    _PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    _PrintWindow.restype = wintypes.BOOL
except Exception:
    _PrintWindow = None

# GDI helpers
_CreateCompatibleDC = gdi32.CreateCompatibleDC
_CreateCompatibleBitmap = gdi32.CreateCompatibleBitmap
_SelectObject = gdi32.SelectObject
_DeleteObject = gdi32.DeleteObject
_DeleteDC = gdi32.DeleteDC
_GetDIBits = gdi32.GetDIBits

BI_RGB = 0
DIB_RGB_COLORS = 0

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def capture_window_printwindow(hwnd: int) -> Optional[np.ndarray]:
    """使用 Win32 PrintWindow + GetDIBits 获取指定窗口的客户端区像素（BGRA -> BGR numpy）。

    返回 None 表示捕获失败，应回退到屏幕抓取。
    """
    if not _PrintWindow:
        return None

    try:
        l, t, r, b = get_client_rect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None

        hdc_window = _GetDC(hwnd)
        if not hdc_window:
            return None

        memdc = _CreateCompatibleDC(hdc_window)
        if not memdc:
            _ReleaseDC(hwnd, hdc_window)
            return None

        hbmp = _CreateCompatibleBitmap(hdc_window, w, h)
        if not hbmp:
            _DeleteDC(memdc)
            _ReleaseDC(hwnd, hdc_window)
            return None

        old_obj = _SelectObject(memdc, hbmp)

        # 使用 PrintWindow 将窗口渲染到我们的内存 DC（memdc）。部分窗口
        # 需要 PW_RENDERFULLCONTENT (2) 才能包含 GPU 渲染的内容；先尝试该
        # 标志，失败时回退到 0。
        ok = False
        try:
            ok = _PrintWindow(hwnd, memdc, 2)
        except Exception:
            try:
                ok = _PrintWindow(hwnd, memdc, 0)
            except Exception:
                ok = False
        if not ok:
            # restore and cleanup
            _SelectObject(memdc, old_obj)
            _DeleteObject(hbmp)
            _DeleteDC(memdc)
            _ReleaseDC(hwnd, hdc_window)
            return None

        # 为 32bpp 准备 BITMAPINFO
        bmi = BITMAPINFO()
        ctypes.memset(ctypes.byref(bmi), 0, ctypes.sizeof(bmi))
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        # 使用正高度以获取自底向上的 DIB；之后我们会垂直翻转
        bmi.bmiHeader.biHeight = h
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        buf_size = w * h * 4
        buf = ctypes.create_string_buffer(buf_size)

        scan_lines = _GetDIBits(hdc_window, hbmp, 0, h, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if scan_lines == 0:
            _SelectObject(memdc, old_obj)
            _DeleteObject(hbmp)
            _DeleteDC(memdc)
            _ReleaseDC(hwnd, hdc_window)
            return None

        # Create numpy array from buffer. DIB is bottom-up by default: flip vertically.
        arr = np.frombuffer(buf, dtype=np.uint8)
        try:
            arr = arr.reshape((h, w, 4))
        except Exception:
            _SelectObject(memdc, old_obj)
            _DeleteObject(hbmp)
            _DeleteDC(memdc)
            _ReleaseDC(hwnd, hdc_window)
            return None

        # Convert BGRA -> BGR and flip vertically because DIB is bottom-up
        arr = arr[::-1, :, :3].copy()

        # If the captured image is mostly black (common for GPU-accelerated
        # windows when PrintWindow cannot capture GPU content), treat as
        # failure so caller can fallback to screen grab.
        try:
            if is_mostly_black(arr):
                _SelectObject(memdc, old_obj)
                _DeleteObject(hbmp)
                _DeleteDC(memdc)
                _ReleaseDC(hwnd, hdc_window)
                return None
        except Exception:
            _SelectObject(memdc, old_obj)
            _DeleteObject(hbmp)
            _DeleteDC(memdc)
            _ReleaseDC(hwnd, hdc_window)
            return None

        # cleanup
        _SelectObject(memdc, old_obj)
        _DeleteObject(hbmp)
        _DeleteDC(memdc)
        _ReleaseDC(hwnd, hdc_window)

        try:
            mn = float(np.min(arr))
            mx = float(np.max(arr))
            mean = float(np.mean(arr))
            # print(f"[捕获] 方法=PrintWindow 形状={arr.shape} 类型={arr.dtype} 最小={mn:.1f} 最大={mx:.1f} 平均={mean:.1f}")
        except Exception:
            # print(f"[捕获] 方法=PrintWindow 形状={getattr(arr,'shape',None)}")
            pass
        return arr
    except Exception:
        return None


def _capture_window_pywin32(hwnd: int) -> Optional[np.ndarray]:
    """使用 pywin32 (BitBlt / PrintWindow) 捕获窗口客户区。

    失败时返回 None；可回退到 ctypes 实现的 `capture_window_printwindow`。
    """
    if not HAS_PYWIN32:
        return None

    try:
        l, t, r, b = get_client_rect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None

        # Get device contexts for client area (GetDC 返回客户区 DC)
        hwnd_dc = win32gui.GetDC(hwnd)
        srcdc = win32ui.CreateDCFromHandle(hwnd_dc)
        memdc = srcdc.CreateCompatibleDC()

        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(srcdc, w, h)
        old_obj = memdc.SelectObject(bmp)

        try:
            # Try PrintWindow first (may work for some windows)
            used_printwindow = False
            try:
                if _PrintWindow:
                    # try PW_RENDERFULLCONTENT first
                    try:
                        res = _PrintWindow(hwnd, memdc.GetSafeHdc(), 2)
                    except Exception:
                        try:
                            res = _PrintWindow(hwnd, memdc.GetSafeHdc(), 0)
                        except Exception:
                            res = False
                    if res:
                        used_printwindow = True
            except Exception:
                used_printwindow = False

            if not used_printwindow:
                # 回退到 BitBlt：从客户区 DC 复制 (源坐标使用 (0,0))
                memdc.BitBlt((0, 0), (w, h), srcdc, (0, 0), win32con.SRCCOPY)

            # Get bitmap bits and convert to numpy
            bits = bmp.GetBitmapBits(True)
            arr = np.frombuffer(bits, dtype=np.uint8)
            try:
                arr = arr.reshape((h, w, 4))
            except Exception:
                return None

            # Convert BGRA -> BGR and flip vertically
            arr = arr[::-1, :, :3].copy()

            # Black-frame detection (treat mostly-black as failure)
            try:
                if is_mostly_black(arr):
                    return None
            except Exception:
                return None

            try:
                mn = float(np.min(arr))
                mx = float(np.max(arr))
                mean = float(np.mean(arr))
                print(f"[捕获] 方法=pywin32 形状={arr.shape} 类型={arr.dtype} 最小={mn:.1f} 最大={mx:.1f} 平均={mean:.1f}")
            except Exception:
                print(f"[捕获] 方法=pywin32 形状={getattr(arr,'shape',None)}")
            return arr
        finally:
            # cleanup GDI objects
            try:
                memdc.SelectObject(old_obj)
            except Exception:
                pass
            try:
                memdc.DeleteDC()
            except Exception:
                pass
            try:
                srcdc.DeleteDC()
            except Exception:
                pass
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
    except Exception:
        return None


# If pywin32 is available, prefer its BitBlt/PrintWindow implementation
if HAS_PYWIN32:
    capture_window_printwindow = _capture_window_pywin32


def letterbox(img: np.ndarray, new_shape=(640, 640), color=(114, 114, 114)) -> Tuple[np.ndarray, Tuple[float, int, int]]:
    """按不变长宽比缩放图像到 `new_shape` (h,w)，并用填充保持目标大小。

    返回 (resized_image, (ratio, pad_top, pad_left))。
    """
    shape = img.shape[:2]  # current shape [h, w]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    new_h, new_w = new_shape

    # scale ratio (new / old)
    r = min(new_h / shape[0], new_w / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw, dh = new_w - new_unpad[0], new_h - new_unpad[1]
    dw //= 2
    dh //= 2

    # resize (use cv2 if available, otherwise basic numpy nearest-neighbor)
    if shape[::-1] != new_unpad:
        if cv2 is not None:
            img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)
        else:
            # simple nearest neighbor resize fallback
            ny, nx = new_unpad[1], new_unpad[0]
            y_idx = (np.linspace(0, img.shape[0] - 1, ny)).astype(int)
            x_idx = (np.linspace(0, img.shape[1] - 1, nx)).astype(int)
            if img.ndim == 3:
                img = img[np.ix_(y_idx, x_idx, range(img.shape[2]))]
            else:
                img = img[np.ix_(y_idx, x_idx)]

    top, bottom = dh, dh + (new_h - new_unpad[1] - dh)
    left, right = dw, dw + (new_w - new_unpad[0] - dw)
    if cv2 is not None:
        img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    else:
        # numpy pad fallback
        pad_top = top
        pad_bottom = bottom
        pad_left = left
        pad_right = right
        if img.ndim == 3:
            img = np.pad(img, ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)), mode="constant", constant_values=tuple(color))
        else:
            img = np.pad(img, ((pad_top, pad_bottom), (pad_left, pad_right)), mode="constant", constant_values=color[0])
    return img, (r, top, left)


def is_mostly_black(arr: np.ndarray, dark_thresh: int = 16, dark_ratio: float = 0.92, std_thresh: float = 12.0) -> bool:
    """如果图像 `arr` 大部分为黑色则返回 True。

    启发式规则：大量像素亮度低于 `dark_thresh`，标准差较小且均值较低。
    该规则针对 GPU 黑帧进行了调整，黑帧可能在边缘存在少量非零像素。
    """
    try:
        if arr is None:
            return True
        # ensure grayscale measure
        a = arr
        if a.size == 0:
            return True
        # compute per-pixel brightness (use max across channels)
        if a.ndim == 3 and a.shape[2] >= 3:
            bright = a.max(axis=2).ravel()
        else:
            bright = a.ravel()
        total = bright.size
        if total == 0:
            return True
        dark = (bright <= dark_thresh).sum()
        dark_frac = dark / float(total)
        mean = float(bright.mean())
        std = float(bright.std())
        if dark_frac >= dark_ratio and mean < (dark_thresh * 2) and std < std_thresh:
            return True
        return False
    except Exception:
        return False


def scale_box(box: Tuple[int, int, int, int], src_shape: Tuple[int, int], dst_shape: Tuple[int, int]) -> Tuple[int, int, int, int]:
    """按比例将盒子 (l,t,r,b) 从源尺寸 src_shape (h,w) 缩放到目标尺寸 dst_shape (h,w)。"""
    src_h, src_w = src_shape
    dst_h, dst_w = dst_shape
    if src_w == 0 or src_h == 0:
        return box
    sx = dst_w / src_w
    sy = dst_h / src_h
    l, t, r, b = box
    return int(l * sx), int(t * sy), int(r * sx), int(b * sy)


# WGC helper removed: using pywin32 BitBlt/PrintWindow implementation below


def find_pids_by_name(name: str) -> List[int]:
    """根据进程名（模糊匹配）返回 PID 列表。"""
    return [
        p.pid
        for p in psutil.process_iter(["name"])
        if p.info["name"] and name.lower() in p.info["name"].lower()
    ]


def find_hwnds_for_pid(pid: int) -> List[int]:
    """枚举 pid 对应的所有可见、有标题的窗口句柄。"""
    hwnds: List[int] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lp):
        _pid = wintypes.DWORD()
        _GetWindowThreadProcessId(hwnd, ctypes.byref(_pid))
        if _pid.value != pid:
            return True
        if not _IsWindowVisible(hwnd):
            return True
        if _GetWindowTextLengthW(hwnd) == 0:
            return True
        hwnds.append(hwnd)
        return True

    _EnumWindows(_cb, 0)
    return hwnds


def get_window_title(hwnd: int) -> str:
    length = _GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def get_client_rect(hwnd: int) -> Tuple[int, int, int, int]:
    """返回窗口客户区在屏幕上的 (left, top, right, bottom)。"""
    cr = wintypes.RECT()
    _GetClientRect(hwnd, ctypes.byref(cr))
    pt = wintypes.POINT(cr.left, cr.top)
    _ClientToScreen(hwnd, ctypes.byref(pt))
    left, top = pt.x, pt.y
    return left, top, left + (cr.right - cr.left), top + (cr.bottom - cr.top)


def find_game_hwnd(proc_name: str = "Client-Win64-Shipping.exe") -> Optional[int]:
    """根据进程名找到游戏窗口句柄（取第一个可见窗口）。"""
    pids = find_pids_by_name(proc_name)
    if not pids:
        return None

    # 获取主显示器大小用于过滤全屏（避免抓到整屏）
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            mon_w, mon_h = mon["width"], mon["height"]
    except Exception:
        mon_w, mon_h = 0, 0

    for pid in pids:
        hwnds = find_hwnds_for_pid(pid)
        for hwnd in hwnds:
            try:
                # 优先用客户区尺寸判断窗口是否合理
                l, t, r, b = get_client_rect(hwnd)
                w, h = r - l, b - t
            except Exception:
                # 回退到顶层窗口矩形
                try:
                    rect = wintypes.RECT()
                    _GetWindowRect(hwnd, ctypes.byref(rect))
                    l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
                    w, h = r - l, b - t
                except Exception:
                    continue

            title = get_window_title(hwnd)
            # 过滤极端大小（接近或等于屏幕大小），避免捕获整屏
            if mon_w and mon_h and (w >= mon_w * 0.98 or h >= mon_h * 0.98):
                # 允许匹配窗口标题明确包含游戏名的情况
                if proc_name.lower().replace('.exe','') in (title or '').lower():
                    return hwnd
                # 否则跳过此 hwnd
                continue

            # 如果尺寸合理，直接返回
            if w > 10 and h > 10:
                return hwnd

    # 无合适 hwnd，返回第一个 pid 的第一个 hwnd 作为最后回退
    first_pid = pids[0]
    first_hwnds = find_hwnds_for_pid(first_pid)
    return first_hwnds[0] if first_hwnds else None


def detect_render_api_by_pid(pid: int) -> Optional[str]:
    """通过进程模块判断渲染 API：返回 'DirectX' / 'Vulkan' / 'OpenGL' / 'Unknown' 或 None（无法访问）。"""
    if not pid:
        return None
    try:
        p = psutil.Process(pid)
        try:
            maps = p.memory_maps()
        except Exception:
            return None

        dll_names = set()
        for m in maps:
            path = getattr(m, "path", None) or getattr(m, "pathname", None) or ""
            if not path:
                try:
                    path = str(m)
                except Exception:
                    path = ""
            name = os.path.basename(path).lower()
            if name:
                dll_names.add(name)

        dx_set = {"d3d9.dll", "d3d11.dll", "d3d12.dll", "dxgi.dll", "d3d8.dll", "d3d10.dll", "d3dcompiler_46.dll", "d3dcompiler_47.dll"}
        vk_set = {"vulkan-1.dll", "vulkanloader.dll"}
        gl_set = {"opengl32.dll", "opengl.dll", "libegl.dll", "libglesv2.dll"}

        if dll_names & dx_set:
            return "DirectX"
        if dll_names & vk_set:
            return "Vulkan"
        if dll_names & gl_set:
            return "OpenGL"

        for nm in dll_names:
            if "vulkan" in nm:
                return "Vulkan"
            if nm.startswith("d3d") or "dxgi" in nm or "d3dcompiler" in nm:
                return "DirectX"
            if "opengl" in nm or "egl" in nm or "gles" in nm:
                return "OpenGL"

        return "Unknown"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception:
        return None


def is_window_borderless_fullscreen(hwnd: int, tolerance: float = 0.98) -> Optional[bool]:
    """判断给定窗口是否为无边框全屏（borderless/fullscreen）。

    逻辑：
    - 通过窗口样式判断是否含有 WS_POPUP 且不包含 WS_OVERLAPPEDWINDOW（常见无边框窗口样式）；
    - 比较窗口客户区或窗口矩形与主显示器分辨率是否接近（>= tolerance）。

    返回 True 表示无边框全屏，False 表示窗口化，None 表示无法判断。
    """
    try:
        if not hwnd:
            return None

        # 获取主显示器大小
        try:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                mon_w, mon_h = mon["width"], mon["height"]
        except Exception:
            mon_w = mon_h = 0

        # 尝试读取窗口样式（GWL_STYLE）
        try:
            GWL_STYLE = -16
            WS_OVERLAPPEDWINDOW = 0x00CF0000
            WS_POPUP = 0x80000000
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        except Exception:
            style = None

        # 获取窗口客户区或窗口矩形尺寸
        try:
            l, t, r, b = get_client_rect(hwnd)
            w, h = r - l, b - t
        except Exception:
            try:
                rect = wintypes.RECT()
                _GetWindowRect(hwnd, ctypes.byref(rect))
                l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
                w, h = r - l, b - t
            except Exception:
                return None

        # 若能获取主显示器尺寸，按相对大小判断是否占满屏幕
        if mon_w and mon_h:
            if w >= mon_w * tolerance and h >= mon_h * tolerance:
                # 若样式为 WS_POPUP 且非标准重叠窗口，视为无边框全屏
                try:
                    if style is not None and (style & WS_POPUP) and not (style & WS_OVERLAPPEDWINDOW):
                        return True
                except Exception:
                    pass
                # 若样式无法判断，仅凭尺寸也可认为是无边框/占满屏幕
                return True

        # 否则视为窗口化
        return False
    except Exception:
        return None


def get_window_dpi_scale(hwnd: Optional[int] = None) -> float:
    """返回窗口的 DPI 缩放因子（physical / logical）。

    优先使用 `GetDpiForWindow`，不存在时回退到屏幕 DPI（GetDeviceCaps）。
    返回 >= 1.0 的缩放因子，出错时返回 1.0。
    """
    try:
        # Windows 10+ API: GetDpiForWindow
        _user32 = ctypes.windll.user32
        if hwnd is not None and hasattr(_user32, 'GetDpiForWindow'):
            try:
                dpi = _user32.GetDpiForWindow(hwnd)
                if dpi and dpi > 0:
                    return float(dpi) / 96.0
            except Exception:
                pass

        # Fallback: use device context DPI (LOGPIXELSX)
        try:
            LOGPIXELSX = 88
            hdc = _user32.GetDC(0)
            # gdi32.GetDeviceCaps
            dpi_x = gdi32.GetDeviceCaps(hdc, LOGPIXELSX)
            if dpi_x and dpi_x > 0:
                return float(dpi_x) / 96.0
        except Exception:
            pass
    except Exception:
        pass
    return 1.0


def capture_window_dxgi(hwnd: int, camera=None) -> Optional[np.ndarray]:
    """使用 DXGI Desktop Duplication 捕获指定窗口的可见区域（DirectX 游戏优化）。
    
    返回 None 表示捕获失败，应回退到 PrintWindow/mss。
    dxcam 捕获主显示器全屏，裁剪到窗口区域。
    """
    # Require an existing camera instance to avoid expensive/verbose
    # creation attempts on every call. Callers should create a camera
    # once via `_create_dxcam_safe()` and pass it in. If `camera` is
    # None we consider DXGI unavailable for this capture call.
    if not HAS_DXCAM or camera is None:
        return None

    try:
        l, t, r, b = get_client_rect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None

        # NOTE: `camera` must be supplied by caller. We avoid creating
        # a camera here to prevent repeated creation attempts and noisy
        # diagnostics on every frame.
        created_camera = False
        try:
            # 获取一帧（全屏幕）
            frame = None
            if hasattr(camera, "get_latest_frame"):
                frame = camera.get_latest_frame()
            # 尝试备用方法
            if frame is None and hasattr(camera, "get_frame"):
                try:
                    frame = camera.get_frame()
                except Exception:
                    frame = None
            if frame is None and hasattr(camera, "grab"):
                try:
                    frame = camera.grab()
                except Exception:
                    frame = None
            # 如果仍然没有帧，短暂等待再重试一次（少量延迟）
            if frame is None:
                try:
                    time.sleep(0.01)
                    if hasattr(camera, "get_latest_frame"):
                        frame = camera.get_latest_frame()
                except Exception:
                    frame = None
            if frame is None:
                if created_camera:
                    try:
                        camera.release()
                    except Exception:
                        pass
                # 记录调试信息
                print("[DXGI] 相机尚无帧可用")
                return None

            # frame 是 numpy 数组，BGR HWC 排列
            # 裁剪到窗口矩形（屏幕坐标）
            frame_h, frame_w, _ = frame.shape
            cl = max(0, l)
            ct = max(0, t)
            cr = min(frame_w, r)
            cb = min(frame_h, b)
            if cl >= cr or ct >= cb:
                camera.release()
                return None

            cropped = frame[ct:cb, cl:cr, :].copy()

            # 检查是否为全黑或有效帧
            if cropped.size == 0 or cropped.max() == 0:
                if created_camera:
                    try:
                        camera.release()
                    except Exception:
                        pass
                return None

            # Caller owns the camera lifecycle; do not release here.
            try:
                mn = float(np.min(cropped))
                mx = float(np.max(cropped))
                mean = float(np.mean(cropped))
                print(f"[捕获] 方法=DXGI 形状={cropped.shape} 类型={cropped.dtype} 最小={mn:.1f} 最大={mx:.1f} 平均={mean:.1f}")
            except Exception:
                print(f"[捕获] 方法=DXGI 形状={getattr(cropped,'shape',None)}")
            return cropped
        except Exception:
            if created_camera:
                try:
                    camera.release()
                except Exception:
                    pass
            return None
    except Exception:
        return None


def debug_list_windows(proc_name: str = "Client-Win64-Shipping.exe") -> None:
    """调试函数：列出匹配进程的 PID、窗口句柄、标题和矩形信息。
    用于诊断无边框/全屏窗口与权限问题。
    """
    print(f"[调试] 正在查找进程名称模式: {proc_name}")
    pids = find_pids_by_name(proc_name)
    print(f"[调试] 匹配到的 PID 列表: {pids}")
    try:
        with mss.mss() as sct:
            mon = sct.monitors[1]
            mon_w, mon_h = mon["width"], mon["height"]
    except Exception:
        mon_w, mon_h = 0, 0
    print(f"[调试] 主显示器分辨率: {mon_w}x{mon_h}")

    for pid in pids:
        print(f"[调试] PID {pid} 的窗口:")
        hwnds = find_hwnds_for_pid(pid)
        for hwnd in hwnds:
            try:
                title = get_window_title(hwnd)
            except Exception:
                title = ""
            try:
                l, t, r, b = get_client_rect(hwnd)
                cw, ch = r - l, b - t
            except Exception:
                try:
                    rect = wintypes.RECT()
                    _GetWindowRect(hwnd, ctypes.byref(rect))
                    l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
                    cw, ch = r - l, b - t
                except Exception:
                    cw, ch = 0, 0
            print(f"  hwnd={hwnd} 标题={title!r} 客户区={cw}x{ch} 矩形=({l},{t},{r},{b})")
            if mon_w and mon_h and (cw >= mon_w * 0.98 or ch >= mon_h * 0.98):
                print("    -> 似乎为全屏或无边框，占据整个显示器")


def diagnose_capture(proc_name: str = "Client-Win64-Shipping.exe") -> bool:
    """诊断当前捕获路径：尝试按优先级使用 DXGI -> PrintWindow -> mss 抓图，
    将结果保存为 outputs/diag_capture.png 并打印统计信息。

    返回 True 表示成功并已保存图片，False 表示捕获失败。
    """
    print(f"[诊断] 正在探测捕获，进程匹配模式: {proc_name}")

    hwnd = find_game_hwnd(proc_name)
    if not hwnd:
        print("[诊断] 未找到匹配的窗口句柄，列出可见窗口以便排查：")
        try:
            debug_list_windows(proc_name)
        except Exception:
            pass
        return False

    # 获取 pid 与渲染 API 提示
    pid = None
    try:
        _pid = wintypes.DWORD()
        _GetWindowThreadProcessId(hwnd, ctypes.byref(_pid))
        pid = _pid.value
    except Exception:
        pid = None

    api = detect_render_api_by_pid(pid) if pid else None
    print(f"[诊断] hwnd={hwnd} pid={pid} 检测到渲染 API: {api!r}")

    img = None
    method = None

    # 尝试 PrintWindow，如失败则尝试使用 mss（并考虑 DPI 缩放）
    img = None
    method = None
    try:
        # 打印客户区坐标以便调试
        try:
            l, t, r, b = get_client_rect(hwnd)
            w, h = r - l, b - t
            print(f"[诊断] 尝试 PrintWindow，客户区逻辑 rect=({l},{t},{w},{h})")
        except Exception:
            pass
        img = capture_window_printwindow(hwnd)
        method = "printwindow"
        if img is not None:
            print(f"[诊断] PrintWindow 成功，shape={getattr(img,'shape',None)}")
        else:
            print(f"[诊断] PrintWindow 返回空（可能被判为黑帧或失败）")
    except Exception:
        img = None

    if img is None:
        try:
            l, t, r, b = get_client_rect(hwnd)
            w, h = r - l, b - t
            if w > 0 and h > 0:
                scale = get_window_dpi_scale(hwnd)
                sl = int(round(l * scale))
                st = int(round(t * scale))
                sw = int(round(w * scale))
                sh = int(round(h * scale))
                # 限制到主显示器范围以避免 mss 报出异常
                try:
                    with mss.mss() as sct:
                        mon = sct.monitors[1]
                        mon_w, mon_h = mon['width'], mon['height']
                except Exception:
                    mon_w = mon_h = 0
                if mon_w and mon_h:
                    if sl < 0:
                        sl = 0
                    if st < 0:
                        st = 0
                    if sl + sw > mon_w:
                        sw = max(0, mon_w - sl)
                    if st + sh > mon_h:
                        sh = max(0, mon_h - st)

                print(f"[诊断] PrintWindow 失败，尝试 mss 抓取，逻辑 rect=({l},{t},{w},{h}) monitor={mon_w}x{mon_h}")
                if w > 0 and h > 0:
                    try:
                        with mss.mss() as sct:
                            # 先尝试不缩放的坐标（get_client_rect 已应返回屏幕坐标）
                            sl, st, sw0, sh0 = l, t, w, h
                            print(f"[诊断] mss 首次尝试（不缩放）物理 rect=({sl},{st},{sw0},{sh0})")
                            raw = sct.grab({"left": sl, "top": st, "width": sw0, "height": sh0})
                            tmp = np.array(raw)[:, :, :3]
                            if tmp.shape[0] == h and tmp.shape[1] == w:
                                img = tmp
                                method = "mss"
                            else:
                                # 再按 DPI 缩放尝试
                                scale = get_window_dpi_scale(hwnd)
                                sl = int(round(l * scale))
                                st = int(round(t * scale))
                                sw = int(round(w * scale))
                                sh = int(round(h * scale))
                                print(f"[诊断] mss 按 DPI 缩放尝试，scale={scale:.3f} 物理 rect=({sl},{st},{sw},{sh})")
                                raw2 = sct.grab({"left": sl, "top": st, "width": sw, "height": sh})
                                img = np.array(raw2)[:, :, :3]
                                method = "mss"
                    except Exception as exc:
                        print(f"[诊断] mss.grab 报错: {exc}")
                        img = None
                else:
                    print(f"[诊断] 物理尺寸不合法，跳过 mss 抓取: sw={sw} sh={sh}")
        except Exception as exc:
            print(f"[诊断] mss 抓取前准备失败: {exc}")
            img = None

    if img is None:
        print("[诊断] 捕获失败：无法获取有效帧")
        return False

    # 保存并打印统计信息
    try:
        os.makedirs("outputs", exist_ok=True)
        out_path = os.path.join("outputs", "diag_capture.png")
        # write image: prefer cv2, then PIL, then numpy fallback
        ok = False
        if cv2 is not None:
            try:
                ok = cv2.imwrite(out_path, img)
            except Exception:
                ok = False
        if not ok:
            try:
                from PIL import Image

                im = Image.fromarray(img[..., ::-1]) if img.shape[2] == 3 else Image.fromarray(img)
                im.save(out_path)
                ok = True
            except Exception:
                ok = False
        if not ok:
            try:
                # 最后手段：保存为 numpy .npy
                np.save(out_path + ".npy", img)
                print(f"[诊断] 已将原始数组保存为 {out_path}.npy")
                ok = True
            except Exception:
                ok = False
        if not ok:
            print(f"[诊断] 保存图片失败: {out_path}")
            return False

        # 统计
        try:
            mn = float(np.min(img))
            mx = float(np.max(img))
            mean = float(np.mean(img))
        except Exception:
            mn = mx = mean = float('nan')

        print(f"[诊断] 已保存 {out_path}，方式: {method}")
        print(f"[诊断] 形状={getattr(img, 'shape', None)} 类型={getattr(img, 'dtype', None)} 最小={mn:.3f} 最大={mx:.3f} 平均={mean:.3f}")
        return True
    except Exception as exc:
        print(f"[诊断] 保存或统计时出错: {exc}")
        return False





# ── 快速自测 ──────────────────────────────────────────────────
if __name__ == "__main__":
    worker = CaptureWorker(proc_name="Client-Win64-Shipping.exe", fps=5)
    worker.start()
    os.makedirs("outputs", exist_ok=True)
    try:
        for i in range(20):
            f = worker.frame
            if f is not None:
                path = f"outputs/cap_{i:03d}.png"
                saved = False
                if cv2 is not None:
                    try:
                        saved = cv2.imwrite(path, f)
                    except Exception:
                        saved = False
                if not saved:
                    try:
                        from PIL import Image

                        im = Image.fromarray(f[..., ::-1]) if f.shape[2] == 3 else Image.fromarray(f)
                        im.save(path)
                        saved = True
                    except Exception:
                        saved = False
                if saved:
                    print(f"[{i}] 已保存 {path}  形状={f.shape}  fps={worker.actual_fps:.1f}")
                else:
                    print(f"[{i}] 保存失败 {path}")
            else:
                print(f"[{i}] 等待中...")
            time.sleep(1)
    finally:
        worker.stop()
