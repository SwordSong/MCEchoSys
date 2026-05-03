"""
overlay.py — 鸣潮强化助手 沉浸式透明悬浮窗模块 (入口文件)

整个模块采用了典型的【前端展示 - 后台运算】多进程架构：
1. 主进程处理 PyQt6 界面渲染，流畅拖拽且不会因为运算“未响应”。
2. 子进程 `_pipeline_process_entry` 负责游戏截屏、YOLO、OCR等消耗资源的重活。
3. 两个进程通过 `mp.Queue` 进行低延迟的消息通信。
"""
import sys
import os
import time
import html
import multiprocessing as mp
from typing import Dict, Any, Optional, List, Set
from src.db import generate_db_write_key
from src.sync import DataSyncWorker

# 动态链接库冲突修复：在 Windows 上，PyQt6 可能会与 onnxruntime 竞争/污染 DLL 加载环境。
# 这里我们需要在导入 PyQt6 相关模块前，先将 onnxruntime 提前导入以锁定所需 DLL 例程。
try:
    import onnxruntime
except ImportError:
    pass

from PyQt6 import QtWidgets, QtCore, QtGui


class SceneMarkerOverlay(QtWidgets.QWidget):
    def __init__(self):
        flags = (
            QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowTransparentForInput
        )
        super().__init__(None, flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._debug_items = []
        self._debug_mode = "none"
        self._frame_size = None
        self._uid_crop_box = None
        self._proc_name = "Client-Win64-Shipping.exe"
        self._last_hwnd = None
        self._last_hwnd_lookup_at = 0.0
        self._debug_visible = os.getenv("MC_SHOW_SCENE_DEBUG", "0") == "1"
        self.hide()

    def _resolve_game_rect(self):
        try:
            from src.capture import find_game_hwnd, get_client_rect
        except Exception:
            return None

        now = time.time()
        if self._last_hwnd is None or now - self._last_hwnd_lookup_at > 1.0:
            self._last_hwnd = find_game_hwnd(self._proc_name)
            self._last_hwnd_lookup_at = now
        if not self._last_hwnd:
            return None
        try:
            return get_client_rect(self._last_hwnd)
        except Exception:
            self._last_hwnd = None
            return None

    def update_scene_debug(self, scene_match: Optional[Dict[str, Any]], frame_shape=None, uid_crop_box=None):
        if not self._debug_visible:
            self._debug_items = []
            self._debug_mode = "none"
            self._frame_size = None
            self._uid_crop_box = None
            self.hide()
            return

        debug_items = []
        debug_mode = "none"
        frame_size = None
        normalized_uid_crop_box = None
        if isinstance(frame_shape, (list, tuple)) and len(frame_shape) >= 2:
            try:
                frame_size = (int(frame_shape[1]), int(frame_shape[0]))
            except Exception:
                frame_size = None
        if isinstance(uid_crop_box, (list, tuple)) and len(uid_crop_box) == 4:
            try:
                normalized_uid_crop_box = tuple(float(v) for v in uid_crop_box)
            except Exception:
                normalized_uid_crop_box = None

        if isinstance(scene_match, dict):
            source = str(scene_match.get("source") or "")
            points = scene_match.get("points") or []
            bbox = scene_match.get("bbox")
            if source == "yolo":
                debug_mode = "bbox"
                if isinstance(points, list) and points:
                    debug_items = list(points)
                elif bbox:
                    debug_items = [{
                        "scene": scene_match.get("scene"),
                        "ui_mode": scene_match.get("ui_mode"),
                        "bbox": bbox,
                        "conf": 1.0,
                        "cls": -1,
                    }]
            elif isinstance(points, list) and points:
                debug_mode = "point"
                debug_items = list(points)

        if not debug_items and normalized_uid_crop_box is None:
            self._debug_items = []
            self._debug_mode = "none"
            self._frame_size = None
            self._uid_crop_box = None
            self.hide()
            return

        game_rect = self._resolve_game_rect()
        if not game_rect:
            self._debug_items = []
            self._debug_mode = "none"
            self._frame_size = None
            self._uid_crop_box = None
            self.hide()
            return

        left, top, right, bottom = game_rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        self.setGeometry(left, top, width, height)
        self._debug_items = list(debug_items)
        self._debug_mode = debug_mode
        self._frame_size = frame_size
        self._uid_crop_box = normalized_uid_crop_box
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, event):
        if not self._debug_items and self._uid_crop_box is None:
            return

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        font = painter.font()
        font.setPointSize(10)
        painter.setFont(font)

        if self._uid_crop_box is not None:
            top, left, bottom, right = self._uid_crop_box
            x1 = int(round(left * self.width()))
            y1 = int(round(top * self.height()))
            x2 = int(round(right * self.width()))
            y2 = int(round(bottom * self.height()))
            uid_pen = QtGui.QPen(QtGui.QColor(64, 160, 255, 220))
            uid_pen.setWidth(2)
            uid_pen.setStyle(QtCore.Qt.PenStyle.DashLine)
            painter.setPen(uid_pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))

            uid_label_rect = QtCore.QRectF(x1, max(0, y1 - 24), 300, 22)
            painter.fillRect(uid_label_rect, QtGui.QColor(0, 0, 0, 160))
            painter.setPen(QtGui.QPen(QtGui.QColor(180, 220, 255)))
            painter.drawText(
                uid_label_rect,
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                f"UID crop ({top:.3f}, {left:.3f}, {bottom:.3f}, {right:.3f})",
            )

        if self._debug_mode == "bbox":
            frame_w = self.width()
            frame_h = self.height()
            if self._frame_size:
                frame_w = max(1, self._frame_size[0])
                frame_h = max(1, self._frame_size[1])
            scale_x = self.width() / max(1, frame_w)
            scale_y = self.height() / max(1, frame_h)

            for index, item in enumerate(self._debug_items):
                bbox = item.get("bbox") or []
                if len(bbox) != 4:
                    continue
                x1 = int(round(float(bbox[0]) * scale_x))
                y1 = int(round(float(bbox[1]) * scale_y))
                x2 = int(round(float(bbox[2]) * scale_x))
                y2 = int(round(float(bbox[3]) * scale_y))
                is_primary = index == 0
                color = QtGui.QColor(80, 220, 120, 230) if is_primary else QtGui.QColor(255, 196, 64, 210)
                outline = QtGui.QPen(color)
                outline.setWidth(3 if is_primary else 2)
                painter.setPen(outline)
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                painter.drawRect(x1, y1, max(1, x2 - x1), max(1, y2 - y1))

                label = f"{item.get('scene', '?')} conf={float(item.get('conf', 0.0)):.2f}"
                text_rect = QtCore.QRectF(x1, max(0, y1 - 24), 260, 22)
                painter.fillRect(text_rect, QtGui.QColor(0, 0, 0, 160))
                painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255)))
                painter.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, label)
            return

        for item in self._debug_items:
            x = int(round(float(item.get("x_ratio", 0.0)) * self.width()))
            y = int(round(float(item.get("y_ratio", 0.0)) * self.height()))
            matched = bool(item.get("matched", False))
            color = QtGui.QColor(60, 220, 120, 220) if matched else QtGui.QColor(255, 80, 80, 235)
            outline = QtGui.QPen(color)
            outline.setWidth(3)
            painter.setPen(outline)
            painter.setBrush(QtGui.QBrush(QtGui.QColor(color.red(), color.green(), color.blue(), 70)))
            painter.drawEllipse(QtCore.QPoint(x, y), 10, 10)
            painter.drawLine(x - 16, y, x + 16, y)
            painter.drawLine(x, y - 16, x, y + 16)

            label = f"{item.get('scene', '?')} ({item.get('x_ratio', 0):.5f}, {item.get('y_ratio', 0):.5f})"
            diffs = item.get("diffs") or []
            if diffs:
                label += f" diff={diffs}"
            text_rect = QtCore.QRectF(x + 14, y - 26, 360, 44)
            painter.fillRect(text_rect, QtGui.QColor(0, 0, 0, 150))
            painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255)))
            painter.drawText(text_rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, label)


# ── 管线子进程入口 ────────────────────────────────────────────
def _pipeline_process_entry(result_queue, stop_event, command_queue=None, db_write_key=None):
    """
    【后台 AI/数据推断 独立进程入口】
    在另一个进程空间跑 PipelineRunner (截屏->YOLO->OCR->入库)。
    这样就不会卡死用户界面。
    参数:
    - result_queue: 结果发回给 UI 界面的通信管道
    - stop_event: 主窗体关闭时的退出信号
    """
    import time

    runner = None

    def _put_latest_result(payload: Dict[str, Any]):
        while not result_queue.empty():
            try:
                result_queue.get_nowait()
            except Exception:
                break
        try:
            result_queue.put_nowait(payload)
        except Exception:
            pass

    def _emit_startup(progress: int, text: str):
        _put_latest_result(
            {
                "status": "startup",
                "startup_progress": max(0, min(100, int(progress))),
                "startup_text": text,
                "tick": 0,
            }
        )

    try:
        _emit_startup(8, "加载管线模块")
        from src.pipeline import PipelineRunner

        use_gpu = os.getenv("MC_USE_GPU", "1") == "1"
        idle_interval = float(os.getenv("MC_IDLE_INTERVAL", "0.3"))
        active_interval = float(os.getenv("MC_ACTIVE_INTERVAL", "0.9"))

        _emit_startup(28, "初始化配置、词典与策略")
        runner = PipelineRunner(use_gpu=use_gpu, db_write_key=db_write_key)

        if not getattr(runner, "_disable_db", False):
            _emit_startup(45, "初始化数据库结构")
            _ = runner.Session
        else:
            _emit_startup(45, "数据库已按配置跳过")

        if not getattr(runner, "_disable_ocr", False):
            _emit_startup(62, "加载 OCR 引擎")
            runner._ensure_ocr()
        else:
            _emit_startup(62, "OCR 已按配置跳过")

        if not getattr(runner, "_capture_only", False):
            _emit_startup(78, "加载场景检测模型")
            runner._ensure_detector()
        else:
            _emit_startup(78, "检测模型已按配置跳过")

        _emit_startup(90, "启动游戏窗口捕获")
        runner.start_capture()
        _emit_startup(96, "资源加载完成，等待首帧")
        print(
            f"{time.time()} [Pipeline-Process] 子进程已启动"
            f" use_gpu={use_gpu}"
            f" idle_interval={idle_interval}"
            f" active_interval={active_interval}"
        )

        idle_count = 0
        force_fast_ticks = 0

        def _handle_command(command: Dict[str, Any]) -> bool:
            if not isinstance(command, dict):
                return False

            cmd_type = str(command.get("type") or "").strip()
            if cmd_type == "switch_strategy_role":
                set_name = str(command.get("set_name") or "").strip()
                role_name = str(command.get("role_name") or "").strip()
                if set_name and role_name:
                    ok = runner.set_strategy_character_for_set(set_name=set_name, role_name=role_name)
                    print(
                        "[Pipeline-Process] switch_strategy_role"
                        f" set={set_name} role={role_name} ok={ok}"
                    )
                    return bool(ok)
            return False

        def _drain_commands() -> bool:
            if command_queue is None:
                return False
            changed = False
            while True:
                try:
                    command = command_queue.get_nowait()
                except Exception:
                    break
                changed = _handle_command(command) or changed
            return changed

        while not stop_event.is_set():
            if _drain_commands():
                # 切角色后至少连续几帧快速刷新，降低“点击后要等很久”的体感。
                force_fast_ticks = max(force_fast_ticks, 3)

            t0 = time.time()
            try:
                result = runner.tick()
                if result is not None:
                    _put_latest_result(result)

                    if result.get("detections"):
                        idle_count = 0
                    else:
                        idle_count += 1
                else:
                    idle_count += 1
            except Exception as e:
                print(f"[Pipeline-Process] tick error: {e}")
                idle_count += 1

            elapsed = time.time() - t0
            # 策略调整：若未匹配到场景(idle)则每0.3s快速重试；若匹配并识别成功则间隔1.5s避免刷屏
            target = idle_interval if (idle_count > 0 or force_fast_ticks > 0) else active_interval
            if force_fast_ticks > 0:
                force_fast_ticks -= 1
            wait = max(0, target - elapsed)
            if wait > 0:
                if command_queue is None:
                    stop_event.wait(wait)
                else:
                    deadline = time.time() + wait
                    while not stop_event.is_set():
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            break
                        stop_event.wait(min(0.08, remaining))
                        if _drain_commands():
                            force_fast_ticks = max(force_fast_ticks, 3)
                            break
    except KeyboardInterrupt:
        pass
    except Exception as e:
        _put_latest_result(
            {
                "status": "startup_error",
                "startup_progress": 100,
                "startup_text": f"启动失败: {e}",
                "tick": 0,
            }
        )
        print(f"[Pipeline-Process] startup error: {e}")
    finally:
        if runner is not None:
            runner.stop()
        print("[Pipeline-Process] 子进程已停止")


class OverlayWindow(QtWidgets.QWidget):
    """不透明、置顶、不抢焦点、可拖动的浮窗。"""

    def __init__(self):
        flags = (
            QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setWindowFlag(QtCore.Qt.WindowType.WindowTransparentForInput, False)
        self.setWindowOpacity(1.0)
        self.setObjectName("OverlayRoot")
        self.setFont(QtGui.QFont("Microsoft YaHei UI", 9))
        self.setWindowTitle("鸣潮 强化助手")
        self.version_label = QtWidgets.QLabel("当前版本主要是收集强化信息，推荐功能还是初级阶段，后续会持续更新完善，欢迎加入交流群反馈建议！")
        self.version_label.setObjectName("VersionLabel")
        self.version_label.setWordWrap(True)
        self._scene_marker_overlay = SceneMarkerOverlay()
        self._title_drag_pos: Optional[QtCore.QPoint] = None

        # ── 标题栏（标题 + 关闭按钮）──
        self.title_label = QtWidgets.QLabel("鸣潮强化助手")
        self.title_label.setObjectName("TitleLabel")

        self.close_btn = QtWidgets.QPushButton("✕")
        self.close_btn.setObjectName("CloseButton")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.close_btn.clicked.connect(self._request_close)

        title_bar = QtWidgets.QHBoxLayout()
        title_bar.setContentsMargins(0, 0, 0, 0)
        title_bar.setSpacing(8)

        title_text = QtWidgets.QVBoxLayout()
        title_text.setContentsMargins(0, 0, 0, 0)
        title_text.setSpacing(2)
        title_text.addWidget(self.title_label)
        title_text.addWidget(self.version_label)

        title_bar.addLayout(title_text, stretch=1)

        title_bar.addStretch()
        title_bar.addWidget(self.close_btn)
        self.title_bar_widget = QtWidgets.QWidget()
        self.title_bar_widget.setObjectName("TitleBar")
        self.title_bar_widget.setLayout(title_bar)
        self.title_bar_widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.title_bar_widget.installEventFilter(self)
        self.title_bar_widget.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)

        # ── 内容控件 ──
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)

        self.role_label = QtWidgets.QLabel("角色配置")
        self.role_label.setObjectName("SectionLabel")
        self.role_label.hide()
        self.role_combo = QtWidgets.QComboBox()
        self.role_combo.setObjectName("RoleCombo")
        self.role_combo.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.role_combo.currentTextChanged.connect(self._on_select_strategy_role)
        self.role_combo.hide()

        role_row = QtWidgets.QHBoxLayout()
        role_row.setContentsMargins(0, 0, 0, 0)
        role_row.setSpacing(6)
        role_row.addWidget(self.role_label)
        role_row.addWidget(self.role_combo, stretch=1)

        self.text_label = QtWidgets.QLabel("程序启动中，请稍等")
        self.text_label.setObjectName("MainPanelLabel")
        self.text_label.setWordWrap(True)

        self.startup_progress = QtWidgets.QProgressBar()
        self.startup_progress.setObjectName("StartupProgress")
        self.startup_progress.setRange(0, 100)
        self.startup_progress.setValue(5)
        self.startup_progress.setTextVisible(False)

        self.startup_progress_label = QtWidgets.QLabel("等待启动管线子进程")
        self.startup_progress_label.setObjectName("StartupProgressLabel")
        self.startup_progress_label.setWordWrap(True)

        self.parsed_label = QtWidgets.QLabel("")
        self.parsed_label.setObjectName("PerfectPanelLabel")
        self.parsed_label.setWordWrap(True)

        self.prob_label = QtWidgets.QLabel("")
        self.prob_label.setObjectName("HiddenLabel")
        self.prob_label.setWordWrap(True)
        self.prob_label.hide()

        self.slot_label = QtWidgets.QLabel("")
        self.slot_label.setObjectName("SlotPanelLabel")
        self.slot_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.slot_label.setWordWrap(True)

        self.advice_label = QtWidgets.QLabel("")
        self.advice_label.setObjectName("AdviceLabel")
        self.advice_label.setWordWrap(True)

        self.echo_section_hdr = QtWidgets.QLabel("声骸信息")
        self.echo_section_hdr.setObjectName("CardHeader")
        self.perfect_section_hdr = QtWidgets.QLabel("推荐配置")
        self.perfect_section_hdr.setObjectName("CardHeader")
        self.slot_section_hdr = QtWidgets.QLabel("辅音状态")
        self.slot_section_hdr.setObjectName("SlotCardHeader")

        vbox = QtWidgets.QVBoxLayout(self)
        vbox.setContentsMargins(12, 10, 12, 10)
        vbox.setSpacing(5)
        vbox.addWidget(self.title_bar_widget)
        vbox.addWidget(self.status_label)
        vbox.addLayout(role_row)
        vbox.addWidget(self.echo_section_hdr)
        vbox.addWidget(self.text_label)
        vbox.addWidget(self.startup_progress)
        vbox.addWidget(self.startup_progress_label)
        vbox.addWidget(self.perfect_section_hdr)
        vbox.addWidget(self.parsed_label)
        vbox.addWidget(self.prob_label)
        vbox.addWidget(self.slot_section_hdr)
        vbox.addWidget(self.slot_label)
        vbox.addWidget(self.advice_label)
        self.setLayout(vbox)

        self.setStyleSheet(
            "QWidget#OverlayRoot {"
            "  background: #171a20;"
            "  border: 1px solid #3d4652;"
            "  border-radius: 8px;"
            "}"
            "QLabel#TitleLabel {"
            "  color: #edf5fb;"
            "  font-size: 15px;"
            "  font-weight: 700;"
            "  letter-spacing: 0px;"
            "}"
            "QLabel#VersionLabel {"
            "  color: #9fb2c2;"
            "  font-size: 10px;"
            "  font-weight: 500;"
            "  line-height: 1.3;"
            "}"
            "QWidget#TitleBar {"
            "  background: #222934;"
            "  border: 1px solid #3b4a59;"
            "  border-radius: 6px;"
            "  padding: 5px 8px;"
            "}"
            "QPushButton#CloseButton {"
            "  color: #ffc9c9;"
            "  background: #421a20;"
            "  border: 1px solid #924149;"
            "  border-radius: 13px;"
            "  font-size: 13px;"
            "  font-weight: 700;"
            "}"
            "QPushButton#CloseButton:hover {"
            "  background: #bd3441;"
            "  color: #ffffff;"
            "  border: 1px solid #e86670;"
            "}"
            "QLabel#StatusLabel {"
            "  color: #9fb2c2;"
            "  font-size: 10px;"
            "  font-family: Consolas, monospace;"
            "  padding: 4px 7px;"
            "  background: #101419;"
            "  border: 1px solid #2a3440;"
            "  border-radius: 5px;"
            "}"
            "QLabel#SectionLabel {"
            "  color: #b4c5d2;"
            "  font-size: 11px;"
            "  font-weight: 600;"
            "  min-width: 64px;"
            "}"
            "QLabel#CardHeader {"
            "  color: #d7e2ea;"
            "  font-size: 11px;"
            "  font-weight: 700;"
            "  padding: 2px 8px;"
            "  border: 1px solid #303946;"
            "  border-left: 3px solid #48c7b7;"
            "  border-radius: 5px;"
            "  background: #20252c;"
            "  margin-top: 4px;"
            "}"
            "QLabel#SlotCardHeader {"
            "  color: #ffe2a0;"
            "  font-size: 13px;"
            "  font-weight: 800;"
            "  padding: 3px 9px;"
            "  border: 1px solid #4f4020;"
            "  border-left: 3px solid #d8a43d;"
            "  border-radius: 5px;"
            "  background: #252015;"
            "  margin-top: 5px;"
            "}"
            "QComboBox#RoleCombo {"
            "  color: #edf5fb;"
            "  background: #19232d;"
            "  border: 1px solid #3f5367;"
            "  border-radius: 6px;"
            "  padding: 4px 8px;"
            "  min-height: 26px;"
            "  font-size: 11px;"
            "}"
            "QComboBox#RoleCombo:hover {"
            "  background: #203140;"
            "  border: 1px solid #56819a;"
            "}"
            "QComboBox#RoleCombo:disabled {"
            "  color: #7a8792;"
            "  background: #171c22;"
            "  border: 1px solid #2d3742;"
            "}"
            "QComboBox#RoleCombo QAbstractItemView {"
            "  color: #edf5fb;"
            "  background: #141b24;"
            "  border: 1px solid #3f5367;"
            "  selection-background-color: #29546a;"
            "  outline: 0px;"
            "}"
            "QLabel#MainPanelLabel {"
            "  color: #e3eff8;"
            "  background: #18202a;"
            "  border: 1px solid #344252;"
            "  border-left: 3px solid #5b8def;"
            "  border-radius: 8px;"
            "  padding: 9px 11px;"
            "  font-size: 12px;"
            "}"
            "QProgressBar#StartupProgress {"
            "  background: #101419;"
            "  border: 1px solid #344252;"
            "  border-radius: 5px;"
            "  min-height: 10px;"
            "  max-height: 10px;"
            "}"
            "QProgressBar#StartupProgress::chunk {"
            "  background: #5b8def;"
            "  border-radius: 4px;"
            "}"
            "QLabel#StartupProgressLabel {"
            "  color: #a9bfd1;"
            "  font-size: 11px;"
            "  padding: 0px 4px 5px 4px;"
            "}"
            "QLabel#PerfectPanelLabel {"
            "  color: #91f0dc;"
            "  background: #10231f;"
            "  border: 1px solid #255247;"
            "  border-left: 3px solid #29c7a9;"
            "  border-radius: 8px;"
            "  padding: 11px 12px;"
            "  font-size: 13px;"
            "  font-weight: 600;"
            "}"
            "QLabel#SlotPanelLabel {"
            "  color: #f0d38f;"
            "  background: #292313;"
            "  border: 1px solid #5a4823;"
            "  border-left: 3px solid #d8a43d;"
            "  border-radius: 8px;"
            "  padding: 11px 13px;"
            "  font-size: 14px;"
            "  font-weight: 600;"
            "}"
            "QLabel#AdviceLabel {"
            "  color: #ffe9a8;"
            "  background: #33251a;"
            "  border: 1px solid #735434;"
            "  border-left: 3px solid #ffbd5b;"
            "  border-radius: 8px;"
            "  padding: 9px 11px;"
            "  font-size: 12px;"
            "  font-weight: 600;"
            "}"
        )
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 5)
        shadow.setColor(QtGui.QColor("#03070c"))
        self.setGraphicsEffect(shadow)

        self.resize(480, 620)
        self.move(72, 72)

        # ── 队列轮询（从子进程接收结果）──
        self._result_queue: Optional[mp.Queue] = None
        self._command_queue: Optional[mp.Queue] = None
        self._sync_worker: Optional[DataSyncWorker] = None
        self._current_strategy_set: str = ""
        self._current_strategy_role_selected: Optional[str] = None
        self._current_strategy_roles: List[str] = []
        self._role_combo_updating = False
        self._pending_strategy_set: str = ""
        self._pending_strategy_role: str = ""
        self._pending_strategy_at: float = 0.0
        self._startup_active = False
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.timeout.connect(self._poll_queue)

        self._show_startup_progress("等待启动管线子进程", 5)
        self.show()

    @staticmethod
    def _html_line(text: str, color: str = "") -> str:
        escaped = html.escape(str(text or "")).replace(" ", "&nbsp;")
        if color:
            return f'<span style="color: {color};">{escaped}</span>'
        return escaped

    @staticmethod
    def _html_slot_line(left: str, right: str = "", color: str = "") -> str:
        left_html = html.escape(str(left or "")).replace(" ", "&nbsp;")
        right_html = html.escape(str(right or "")).replace(" ", "&nbsp;")
        color_style = f"color: {color};" if color else ""
        return (
            '<table width="100%" cellspacing="0" cellpadding="0">'
            "<tr>"
            f'<td><span style="{color_style}">{left_html}</span></td>'
            f'<td align="right"><span style="{color_style}">{right_html}</span></td>'
            "</tr>"
            "</table>"
        )

    @staticmethod
    def _html_main_panel(left_lines: List[str], right_lines: List[str]) -> str:
        def render_lines(lines: List[str]) -> str:
            return "<br>".join(
                html.escape(str(line or "")).replace(" ", "&nbsp;")
                for line in lines
            )

        left_html = render_lines(left_lines)
        if not right_lines:
            return left_html

        right_html = render_lines(right_lines)
        return (
            '<table width="100%" cellspacing="0" cellpadding="0">'
            "<tr>"
            f'<td valign="top">{left_html}</td>'
            '<td valign="top" align="right" style="color:#a9bfd1; padding-left:12px;">'
            f"{right_html}"
            "</td>"
            "</tr>"
            "</table>"
        )

    def _set_startup_mode(self, enabled: bool):
        self._startup_active = bool(enabled)
        self.startup_progress.setVisible(enabled)
        self.startup_progress_label.setVisible(enabled)

        self.perfect_section_hdr.setVisible(not enabled)
        self.parsed_label.setVisible(not enabled)
        self.slot_section_hdr.setVisible(not enabled)
        self.slot_label.setVisible(not enabled)
        self.advice_label.setVisible(not enabled)
        self.prob_label.hide()

        if enabled:
            self.role_label.hide()
            self.role_combo.hide()

    def _show_startup_progress(self, text: str, progress: Any = 0, title: str = "程序启动中，请稍等"):
        self._set_startup_mode(True)
        try:
            value = int(float(progress))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(100, value))
        self.text_label.setText(title)
        self.startup_progress.setValue(value)
        self.startup_progress_label.setText(str(text or "正在准备资源"))
        self.parsed_label.setText("")
        self.prob_label.setText("")
        self.slot_label.setText("")
        self.advice_label.setText("")

    @staticmethod
    def _flatten_priority_names(perfect_consonant: Dict[str, Any]) -> Set[str]:
        names: Set[str] = set()

        def collect(value: Any):
            if isinstance(value, str):
                name = value.strip()
                if name:
                    names.add(name)
                return
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
                return
            if isinstance(value, (list, tuple, set)):
                for item in value:
                    collect(item)

        collect(perfect_consonant if isinstance(perfect_consonant, dict) else {})
        return names

    @staticmethod
    def _priority_substat_variant_count(name: str) -> int:
        stat_name = str(name or "").strip()
        if stat_name in {"攻击", "防御"}:
            return 1
        if stat_name == "生命":
            return 2
        return 1

    @staticmethod
    def _matches_priority_substat(name: Any, is_pct: Any, priority_names: Set[str]) -> bool:
        stat_name = str(name or "").strip()
        if not stat_name or stat_name not in priority_names:
            return False
        if stat_name in {"攻击", "防御"}:
            return is_pct is True
        return True

    # ── 标题栏拖动实现 ──
    def eventFilter(self, obj, event):
        if obj is self.title_bar_widget:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._title_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QtCore.QEvent.Type.MouseMove and self._title_drag_pos is not None:
                if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._title_drag_pos)
                return True
            if event.type() in (QtCore.QEvent.Type.MouseButtonRelease, QtCore.QEvent.Type.MouseButtonDblClick):
                self._title_drag_pos = None
                return True
        return super().eventFilter(obj, event)

    # ── 子进程队列轮询 ──
    def start_polling(
        self,
        queue: mp.Queue,
        command_queue: Optional[mp.Queue] = None,
        sync_worker: Optional[DataSyncWorker] = None,
        interval_ms: int = 250,
    ):
        """
        供主进程外部调用。通过启动 QTimer 每秒4次的频率检查 Queue。
        这是在不卡死 UI 主线程前提下获取后台并发数据的经典 PyQt 写法。
        """
        self._result_queue = queue
        self._command_queue = command_queue
        self._sync_worker = sync_worker
        self._poll_timer.start(interval_ms)

    def _get_server_counts(self) -> Dict[str, Any]:
        if self._sync_worker is None:
            return {}
        try:
            return self._sync_worker.get_server_counts()
        except Exception:
            return {}

    def _clear_pending_strategy_role(self):
        self._pending_strategy_set = ""
        self._pending_strategy_role = ""
        self._pending_strategy_at = 0.0

    def _set_strategy_role_state(self, set_name: str, selected: Optional[str], roles: List[str]):
        self._current_strategy_set = set_name
        self._current_strategy_roles = list(roles)
        self._current_strategy_role_selected = selected

        now = time.time()
        if self._pending_strategy_set and self._pending_strategy_role:
            if self._pending_strategy_set != set_name:
                self._clear_pending_strategy_role()
            elif selected == self._pending_strategy_role:
                self._clear_pending_strategy_role()
            elif now - self._pending_strategy_at > 2.5:
                self._clear_pending_strategy_role()

        display_selected = selected
        if (
            self._pending_strategy_set == set_name
            and self._pending_strategy_role
            and self._pending_strategy_role in roles
            and self._pending_strategy_role != selected
        ):
            # 用户刚切换时，先保持下拉框停在用户选择，避免被旧帧瞬间回滚。
            display_selected = self._pending_strategy_role

        if roles:
            if display_selected not in roles:
                display_selected = roles[0]
            self._role_combo_updating = True
            try:
                with QtCore.QSignalBlocker(self.role_combo):
                    self.role_combo.clear()
                    self.role_combo.addItems(roles)
                    self.role_combo.setCurrentText(display_selected)
                self.role_label.setVisible(True)
                self.role_combo.setVisible(True)
                self.role_combo.setEnabled(len(roles) > 1)
            finally:
                self._role_combo_updating = False
        else:
            self._clear_pending_strategy_role()
            self._role_combo_updating = True
            try:
                with QtCore.QSignalBlocker(self.role_combo):
                    self.role_combo.clear()
                self.role_label.hide()
                self.role_combo.hide()
            finally:
                self._role_combo_updating = False

    def _on_select_strategy_role(self, role_name: str):
        if self._role_combo_updating:
            return
        if not self._command_queue:
            return
        if not self._current_strategy_set:
            return
        role_name = str(role_name or "").strip()
        if not role_name:
            return
        if role_name not in self._current_strategy_roles:
            return
        if role_name == (self._current_strategy_role_selected or ""):
            return

        payload = {
            "type": "switch_strategy_role",
            "set_name": self._current_strategy_set,
            "role_name": role_name,
        }
        try:
            self._command_queue.put_nowait(payload)
        except Exception:
            return

        # 记录待确认切换状态，等待子进程下一帧确认。
        self._pending_strategy_set = self._current_strategy_set
        self._pending_strategy_role = role_name
        self._pending_strategy_at = time.time()

    def _poll_queue(self):
        """定时器执行函数，不断尝试拿出最新的通信队列数据抛给 _on_update 绘制界面"""
        if not self._result_queue:
            return
        data = None
        try:
            # 清理旧数据，只拿最新发送的一条帧回报
            while not self._result_queue.empty():
                data = self._result_queue.get_nowait()
        except Exception:
            pass
        if data is not None:
            self._on_update(data)

    def _request_close(self):
        """用户点击红底 x 按钮执行，发送命令销毁 QApplication 对象从而优雅退出进程"""
        app = QtWidgets.QApplication.instance()
        if app:
            app.quit()

    def _on_update(self, data: Dict[str, Any]):
        """
        核心渲染桥梁函数。接收 AI 子进程传过来的 Python 字典。
        根据其中的 status 状态、OCR 文字、贝叶斯概率、策略建议来变色和赋值 QLabel (文本标签)。
        """
        status = data.get("status", "running")
        latency = data.get("latency_ms", 0)
        fps = data.get("capture_fps", 0)
        tick = data.get("tick", 0)
        uid = data.get("uid") or "-"
        account_id = data.get("account_id")
        acct_text = str(account_id) if account_id is not None else "-"
        client_pid = data.get("client_pid")
        pid_text = str(client_pid) if client_pid is not None else "-"
        cfg_status = data.get("strategy_config", {})
        cfg_error_count = int(cfg_status.get("error_count", 0))
        cfg_badge = f" 配置异常={cfg_error_count}" if cfg_error_count > 0 else ""

        pending_badge = ""
        if self._pending_strategy_set and self._pending_strategy_role:
            if time.time() - self._pending_strategy_at <= 2.5:
                pending_badge = f"  切换中->{self._pending_strategy_role}"
            else:
                self._clear_pending_strategy_role()
        self.status_label.setText(
            f"状态={status}  UID={uid}  账号={acct_text}  PID={pid_text}{cfg_badge}{pending_badge}  tick={tick}  延迟={latency}ms  采集={fps}fps"
        )
        self._scene_marker_overlay.update_scene_debug(
            data.get("scene_match"),
            data.get("frame_shape"),
            data.get("uid_crop_box"),
        )

        if status in ("startup", "startup_error"):
            self._scene_marker_overlay.update_scene_debug(None)
            self._clear_pending_strategy_role()
            title = "程序启动失败" if status == "startup_error" else "程序启动中，请稍等"
            self._show_startup_progress(
                data.get("startup_text") or "正在准备资源",
                data.get("startup_progress", 0),
                title=title,
            )
            return

        if self._startup_active:
            self._set_startup_mode(False)
        
        # 增加右侧强化的统计显示
        account_stats = data.get("account_stats")
        enh_stats_lines: List[str] = []
        if account_stats:
            total_enh = account_stats.get('total_enhance', 0)
            today_enh = account_stats.get('today_enhance', 0)
            client_enh = account_stats.get('client_enhance', 0)
            global_total_enh = account_stats.get('global_total_enhance', 0)
            global_today_enh = account_stats.get('global_today_enhance', 0)
            server_counts = self._get_server_counts()
            if server_counts.get("total_count") is not None:
                global_total_enh = server_counts.get("total_count", global_total_enh)
            if server_counts.get("today_count") is not None:
                global_today_enh = server_counts.get("today_count", global_today_enh)
            contrib_rate = account_stats.get('contribution_rate', 0.0)
            if global_total_enh:
                try:
                    contrib_rate = float(total_enh or 0) / float(global_total_enh)
                except (TypeError, ValueError, ZeroDivisionError):
                    contrib_rate = 0.0
            contrib_str = f"{contrib_rate * 100:.2f}%" if global_total_enh > 0 else "0.00%"
            
            enh_stats_lines = [
                f"总强化:{total_enh}",
                f"今日:{today_enh}",
                f"客户端:{client_enh}",
                f"大数据统计:{global_total_enh}",
                f"大数据今日:{global_today_enh}",
                f"贡献率:{contrib_str}",
            ]

        if status == "waiting_game_process":
            self._scene_marker_overlay.update_scene_debug(None)
            self._set_strategy_role_state("", None, [])
            self._clear_pending_strategy_role()
            self.text_label.setText(self._html_main_panel(["等待游戏启动（每3秒轮询）"], enh_stats_lines))
            self.parsed_label.setText("")
            self.prob_label.setText("")
            self.slot_label.setText("")
            self.advice_label.setText("建议: 启动鸣潮并进入可识别界面")
            return

        if status == "waiting_uid":
            self._scene_marker_overlay.update_scene_debug(None, data.get("frame_shape"), data.get("uid_crop_box"))
            self._set_strategy_role_state("", None, [])
            self._clear_pending_strategy_role()
            uid_status = data.get("uid_status", {})
            wait_seconds = uid_status.get("uid_wait_seconds", 0)
            consistent = uid_status.get("uid_consistent", 0)
            required = uid_status.get("uid_required", 3)
            uid_debug = uid_status.get("uid_debug", {}) or {}
            raw_texts = uid_debug.get("raw_texts") or []
            normalized_texts = uid_debug.get("normalized_texts") or []
            digit_stream = uid_debug.get("digit_stream") or "-"
            candidate = uid_debug.get("digit_candidate") or "-"
            crop_shape = uid_debug.get("crop_shape")
            self.text_label.setText(self._html_main_panel(["等待识别右下角UID..."], enh_stats_lines))
            self.parsed_label.setText(
                "\n".join(
                    [
                        f"UID连续一致次数: {consistent}/{required}",
                        f"裁剪尺寸: {crop_shape}",
                        f"候选9位: {candidate}",
                        f"OCR原文: {' | '.join(str(t) for t in raw_texts[:3]) if raw_texts else '(空)'}",
                    ]
                )
            )
            self.prob_label.setText("")
            self.slot_label.setText("")
            if cfg_error_count > 0:
                first_err = (cfg_status.get("errors") or ["配置异常"]) [0]
                self.advice_label.setText(f"配置警告: {first_err}")
            else:
                self.advice_label.setText(f"建议: 保持游戏主界面可见，约 {wait_seconds}s 后重试")
            return



        echo = data.get("echo")
        if not echo:
            self._set_strategy_role_state("", None, [])
            self.text_label.setText(self._html_main_panel(["未检测到强化面板"], enh_stats_lines))
            self.parsed_label.setText("")
            self.prob_label.setText("")
            self.slot_label.setText("")
            self.advice_label.setText("")
            return

        echo_name = str(echo.get("echo_name") or "")
        level_text = str(echo.get("level_text") or "")
        cost_raw = echo.get("cost")
        cost = str(cost_raw) if cost_raw is not None else ""
        set_name = str(echo.get("set_name") or "")
        main_stat = str(echo.get("main_stat") or "")
        echo_instance_id = str(echo.get("echo_instance_id") or "-")

        strategy_role = echo.get("strategy_role") or {}
        role_set_name = str(strategy_role.get("set_name") or set_name)
        role_selected_raw = strategy_role.get("selected")
        role_selected = str(role_selected_raw) if role_selected_raw else None
        role_available = [str(x) for x in (strategy_role.get("available") or []) if str(x)]
        self._set_strategy_role_state(role_set_name, role_selected, role_available)

        # 把强化统计信息加在主面板信息的右侧/下方
        main_panel_lines = [
            "主面板:",
            f"声骸: {echo_name} {level_text}  COST {cost}",
            f"实例ID: {echo_instance_id}",
            f"套装: {set_name}",
            f"主词条: {main_stat}",
        ]
        self.text_label.setText(
            self._html_main_panel(main_panel_lines, enh_stats_lines)
        )
        perfect = echo.get("perfect_recommendation") or {}
        perfect_cost = perfect.get("cost_main_stats") if isinstance(perfect, dict) else {}
        perfect_consonant = perfect.get("consonant") if isinstance(perfect, dict) else {}
        if not isinstance(perfect_cost, dict):
            perfect_cost = {}
        if not isinstance(perfect_consonant, dict):
            perfect_consonant = {}

        cost_digits = "".join(ch for ch in str(cost) if ch.isdigit())
        cost_key = f"COST{cost_digits}" if cost_digits else ""
        perfect_lines: List[str] = ["推荐配置:"]
        has_perfect = False
        has_cost_line = False

        if perfect_cost and cost_key:
            values = [str(v).strip() for v in (perfect_cost.get(cost_key) or []) if str(v).strip()]
            has_cost_line = True
            if values:
                has_perfect = True
                perfect_lines.append(f"主词条推荐: {' / '.join(values)}")
            else:
                perfect_lines.append(f"主词条推荐: 暂无 {cost_key} 配置")

        if perfect_consonant:
            perfect_lines.append("辅音优先级:")

            def _tier_sort_key(raw_key: str):
                key_text = str(raw_key)
                try:
                    return (0, int(key_text))
                except Exception:
                    return (1, key_text)

            for tier_key in sorted(perfect_consonant.keys(), key=_tier_sort_key):
                values = [str(v).strip() for v in (perfect_consonant.get(tier_key) or []) if str(v).strip()]
                if values:
                    has_perfect = True
                    perfect_lines.append(f"  {tier_key}档: {' / '.join(values)}")

        if not has_perfect and not has_cost_line:
            perfect_lines.append("暂无该套装角色的推荐配置")

        self.parsed_label.setText("\n".join(perfect_lines))
        self.prob_label.setText("")

        slots = echo.get("slots") or []
        substat_posterior = echo.get("substat_posterior") or {}
        if not isinstance(substat_posterior, dict):
            substat_posterior = {}
        substat_predictions = substat_posterior.get("predictions") or []
        if not isinstance(substat_predictions, list):
            substat_predictions = []
        priority_names = self._flatten_priority_names(perfect_consonant)
        slot_active_color = "#f0d38f"
        slot_prediction_color = "#d8ecff"
        slot_no_effect_color = "#8b9298"
        slot_lines = []
        
        # 计算剩余未开孔中“有效辅音”的纯数学概率。
        # 配置里的“攻击/防御”按百分比处理，固定攻击/防御不算有效辅音。
        initial_valid = 0
        for p_name in priority_names:
            initial_valid += self._priority_substat_variant_count(p_name)
                
        rolled_names = []
        valid_rolled_count = 0
        for s in slots:
            s_name = str(s.get("name") or "").strip()
            if s_name:
                rolled_names.append(s_name)
                # 如果这个已开词条正好属于我们要的优先词条列表中，对应扣除一个可出词条数。
                if self._matches_priority_substat(s_name, s.get("is_pct"), priority_names):
                    valid_rolled_count += 1
                    
        pool_size = max(1, 13 - len(rolled_names))
        valid_count = max(0, initial_valid - valid_rolled_count)
        unrolled_slots = 5 - len(rolled_names)
        
        prob_text = ""
        remaining_single_prob = None
        if unrolled_slots > 0 and priority_names:
            single_prob = valid_count / pool_size
            remaining_single_prob = single_prob
            import math
            invalid_count = pool_size - valid_count
            prob_all_invalid = (math.comb(invalid_count, unrolled_slots) / math.comb(pool_size, unrolled_slots)) if invalid_count >= unrolled_slots else 0.0
            prob_at_least_one = 1.0 - prob_all_invalid
            
            if unrolled_slots == 1:
                prob_text = f"  (单孔有效率: {single_prob*100:.1f}%)"
            else:
                prob_text = f"  (单孔有效率: {single_prob*100:.1f}% | 剩余{unrolled_slots}孔至少出1: {prob_at_least_one*100:.1f}%)"
                
        slot_lines.append(self._html_line(f"当前辅音状态:{prob_text}", slot_active_color))
        if substat_posterior.get("skipped"):
            skip_reason = str(substat_posterior.get("reason") or "无剩余孔位可预测")
            slot_lines.append(self._html_line(f"大数据推断: {skip_reason}", slot_prediction_color))
        elif substat_predictions:
            prediction_parts = []
            for item in substat_predictions[:5]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("key") or "").strip()
                if not name:
                    continue
                try:
                    prob = float(item.get("probability", 0.0))
                except (TypeError, ValueError):
                    prob = 0.0
                prediction_parts.append(f"{name} {prob * 100:.1f}%")
            if prediction_parts:
                slot_lines.append(self._html_line(f"大数据推断: {' / '.join(prediction_parts)}", slot_prediction_color))
        
        for i in range(1, 6):
            slot = next((s for s in slots if int(s.get("index", 0)) == i), {})
            name = slot.get("name")
            value_text = slot.get("value_text") or ""
            text = f"{name} {value_text}".strip() if name else (slot.get("text") or "-")
            quality = slot.get("quality")
            if quality:
                text = f"{text} [{quality}]"
            is_no_effect = bool(name and priority_names and not self._matches_priority_substat(name, slot.get("is_pct"), priority_names))
            color = slot_no_effect_color if is_no_effect else slot_active_color
            if name:
                right_text = "有效" if not is_no_effect else "无效果"
            elif remaining_single_prob is not None:
                right_text = f"出现率 {remaining_single_prob * 100:.1f}%"
            else:
                right_text = ""
            slot_lines.append(self._html_slot_line(f"[{i}] {text}", right_text, color))
        if cfg_error_count > 0:
            cfg_errors = cfg_status.get("errors") or []
            slot_lines.append(self._html_line("配置警告:", slot_active_color))
            for msg in cfg_errors[:2]:
                slot_lines.append(self._html_line(f"  - {msg}", slot_active_color))
        self.slot_label.setText("<br>".join(slot_lines) if slot_lines else "")

        advice = echo.get("advice") or {}
        advice_text = advice.get("text") or "-"
        reason = advice.get("reason") or ""
        cost_analysis = advice.get("cost_analysis") or {}
        if not isinstance(cost_analysis, dict):
            cost_analysis = {}
        score_text = ""
        if advice.get("recommended_action") in ("single", "multi"):
            score_text = (
                f"  单开={float(advice.get('single_score', 0.0)):.2f}"
                f"  多开={float(advice.get('multi_score', 0.0)):.2f}"
            )
        cost_summary = str(cost_analysis.get("summary") or "")
        suggestions = [str(x).strip() for x in (cost_analysis.get("suggestions") or []) if str(x).strip()]
        suggestion_text = f"建议操作: {' / '.join(suggestions[:3])}" if suggestions else ""
        self.advice_label.setText(
            "\n".join(
                line for line in [
                    f"强化建议: {advice_text}{score_text}",
                    reason,
                    cost_summary,
                    suggestion_text,
                ] if line
            )
        )


# ── 独立启动入口（管线在子进程中运行）──────────────────────────
def main():
    mp.freeze_support()

    # 通过 multiprocessing 启动一个独立的子进程来跑 PipelineRunner，主进程专注于 PyQt 界面渲染和用户交互。
    app = QtWidgets.QApplication(sys.argv)
    overlay = OverlayWindow()

    result_queue = mp.Queue(maxsize=8)
    command_queue = mp.Queue(maxsize=16)
    stop_event = mp.Event()
    db_write_key = generate_db_write_key()
    # 启动管线子进程，传入通信队列和退出事件
    proc = mp.Process(
        target=_pipeline_process_entry,
        args=(result_queue, stop_event, command_queue, db_write_key),
        daemon=True,
        name="PipelineProcess",
    )
    proc.start()
    overlay._show_startup_progress(f"管线子进程已启动，PID={proc.pid}", 10)
    _now = time.time()
    _ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_now))
    print(f"[{_ts},{int((_now % 1) * 1000):03d}] [Overlay] 管线子进程已启动 pid={proc.pid}")

    # 启动云端大数据同步
    sync_worker = DataSyncWorker(interval_sec=30, db_write_key=db_write_key)
    # 启动 UI 定时器轮询子进程结果队列，更新界面显示；同时读取同步线程返回的云端统计
    overlay.start_polling(
        result_queue,
        command_queue=command_queue,
        sync_worker=sync_worker,
        interval_ms=250,
    )
    sync_worker.start()

    def cleanup():
        overlay._poll_timer.stop()
        overlay._scene_marker_overlay.hide()
        print("[Overlay] 正在停止管线子进程...")
        stop_event.set()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
            
        print("[Overlay] 正在停止云端同步...")
        sync_worker.stop()
        sync_worker.join(timeout=2)
        print("[Overlay] 已退出")

    app.aboutToQuit.connect(cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
