"""
pipeline.py — 端到端管线：捕获 → 检测 → 裁剪 → OCR → 解析 → 概率 → 入库。
提供 PipelineRunner 可被浮窗或外部循环调用。
"""
import datetime
import json
import os
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Optional, Callable, List, Dict, Any, Tuple

import psutil

from src.capture import CaptureWorker, is_window_borderless_fullscreen
from src.detect_feature_code import detect_uid_value_with_debug, crop_region_by_box, get_uid_crop_box
from src.observation_extractor import ObservationExtractor, EchoObservation
from src.preprocess import enhance_for_ocr, crop_bbox, upscale_if_small
from src.parser import parse_texts
from src.probability import FrequencyModel, BayesianUpdater, ActionStrategyAdvisor, SubstatPosteriorModel
from src.recognition_debug import RecognitionDebugDumper
from src.resources import resource_path, writable_data_path
from src.strategy_config import load_strategy_priority_profile_with_meta
from src.db import (
    Account,
    EchoInfo,
    EchoSubstat,
    build_echo_instance_id,
    ensure_account_hash,
    generate_db_write_key,
    local_now,
    local_machine_name,
    init_db,
    make_uuid,
    mark_client_started,
)

def group_ocr_texts_by_y(ocr_results: List[Tuple[list, str, float]], y_threshold: int = 15) -> List[str]:
    """
    将 recognize_with_boxes 的输出按 Y 轴聚类（行），每行内按 X 轴排序，
    从而解决 YOLO BBox 捕获带来的 Y 轴随机抖动/词条顺序错乱问题。
    :param ocr_results: [(bbox_points, text, conf), ...]
    :param y_threshold: 同一行的最大 Y 坐标差值
    """
    if not ocr_results:
        return []

    # 1. 计算每个文本框的平均 Y 坐标和最小 X 坐标
    items = []
    for box, text, conf in ocr_results:
        # box: [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
        y_coords = [p[1] for p in box]
        x_coords = [p[0] for p in box]
        avg_y = sum(y_coords) / len(y_coords)
        min_x = min(x_coords)
        items.append({
            "box": box,
            "text": text,
            "conf": conf,
            "y": avg_y,
            "x": min_x
        })

    # 2. 按 Y 坐标粗略排序
    items.sort(key=lambda i: i["y"])

    # 3. 按 Y 轴阈值进行同行聚类
    rows = []
    if items:
        current_row = [items[0]]
        for i in range(1, len(items)):
            if items[i]["y"] - current_row[-1]["y"] <= y_threshold:
                current_row.append(items[i])
            else:
                current_row.sort(key=lambda i: i["x"])
                rows.append(current_row)
                current_row = [items[i]]
        current_row.sort(key=lambda i: i["x"])
        rows.append(current_row)

    # 4. 组装成纯文本并附带排版打印
    final_texts = []
    for idx, row in enumerate(rows):
        row_text = " ".join([item["text"] for item in row])
        avg_y = sum([item["y"] for item in row]) / len(row)
        print(f"[OCR 行列分析] Row {idx+1} (Y≈{avg_y:.1f}): {row_text}")
        final_texts.append(row_text)
    
    return final_texts


@dataclass(frozen=True)
class GameProcessSnapshot:
    pid: int
    started_at: datetime.datetime
    captured_at: float


@dataclass
class ActiveEchoContext:
    local_id: str
    anchor_scene: str
    obs: EchoObservation
    last_seen_at: float
    pending_signature: Optional[Tuple[str, ...]] = None
    pending_count: int = 0


class PipelineRunner:
    _DEFAULT_YOLO_CLASSES = ["echo_panel", "enhance_panel"]

    """
    封装完整采集→推断流程，已优化为高性能低CPU模式：
    Capture(内存直读) -> Crop(切片) -> Preprocess(增强) -> OCR(推理)
    每次 tick() 返回结果字典。
    """

    # 初始化管线组件，加载策略配置和面板布局，准备数据库连接。
    def __init__(
        self,
        proc_name: str = "Client-Win64-Shipping.exe",# 目标游戏进程名，用于捕获线程定位窗口
        model_path: Optional[str] = None,# 检测模型路径，默认为 None 以使用 Detector 内置默认模型
        use_gpu: bool = True,# 是否启用 GPU 加速（默认 True，浮窗模式建议开启以减轻 CPU 负担，前提是环境配置正确）
        db_path: Optional[str] = None, # 数据库连接字符串；默认使用用户数据目录下的 SQLite 文件
        on_result: Optional[Callable[[Dict[str, Any]], None]] = None, # 结果回调函数，供 UI 使用
        game_day_reset_hour: int = 4, # 游戏内一天重置的小时（0-23），用于区分不同游戏日的记录
        strategy_config_path: str = "data/strategy_priority.json",# 策略优先级配置路径
        echo_dictionary_path: str = "data/echo_dictionary.json",# 观测抽取用的词典数据路径
        substat_values_path: str = "data/substat_values.json",  # 观测抽取用的词典数据路径
        panel_layout_path: str = "data/panel_layout.json", # 面板布局配置路径
        db_write_key: Optional[str] = None, # 本次程序初始化生成的数据库写入密钥
    ):
        if not use_gpu:
            print("[Pipeline] GPU-only mode enabled, forcing use_gpu=True")
            use_gpu = True
        self.proc_name = proc_name
        # 通过环境变量控制各阶段功能开关，方便调试和性能优化
        self._capture_only = os.getenv("MC_CAPTURE_ONLY", "0") == "1"  # capture_only 模式仅运行捕获线程，跳过后续处理，适用于性能测试和调试捕获链路。
        # 是否禁用结果复用机制，即使画面未发生变化也强制重新 OCR 和解析，适用于调试 OCR 稳定性和实时性，但会增加 CPU 占用。
        self._disable_result_reuse = os.getenv("MC_DISABLE_RESULT_REUSE", "0") == "1"
        # minimal_payload 模式仅返回核心识别结果，省略额外的调试信息和中间数据，适用于对性能要求较高的场景或生产环境，但不利于调试和分析。
        self._minimal_payload = os.getenv("MC_MINIMAL_PAYLOAD", "0") == "1"
        # OCR 相关的时间间隔控制，避免过于频繁地进行 OCR 识别，降低 CPU 占用，同时也提供环境变量以便调整和测试不同的频率。
        self._uid_ocr_interval = float(os.getenv("MC_UID_OCR_INTERVAL", "1.0"))
        # 由于 UID 识别通常比较稳定，过于频繁的 OCR 可能导致不必要的性能开销，因此提供独立的开关来控制是否启用 UID 相关的 OCR 和重试机制。
        self._panel_ocr_interval = float(os.getenv("MC_PANEL_OCR_INTERVAL", "1.0"))
        # OCR 识别通常是性能瓶颈，提供独立的开关来控制是否启用 OCR 功能，以便在性能受限的环境中禁用 OCR 相关的处理逻辑。
        self._disable_ocr = os.getenv("MC_DISABLE_OCR", "0") == "1"
        self._disable_uid = os.getenv("MC_DISABLE_UID", "0") == "1"
        self._disable_uid_recheck = os.getenv("MC_DISABLE_UID_RECHECK", "1") == "1"
        self._disable_panel_ocr = self._disable_ocr or os.getenv("MC_DISABLE_PANEL_OCR", "0") == "1"
        # 由于策略模块通常包含业务逻辑和数据库访问，热重载可能导致状态不一致或资源泄漏，因此提供独立的开关来控制是否启用策略热重载。
        self._disable_strategy = os.getenv("MC_DISABLE_STRATEGY", "0") == "1"
        # 数据库访问通常涉及连接池和会话管理，热重载可能导致连接泄漏或状态不一致，因此提供独立的开关来控制是否启用数据库热重载。
        self._disable_db = os.getenv("MC_DISABLE_DB", "0") == "1"
        # 是否启用阶段耗时统计，默认为 True 以便调试和性能分析，生产环境可通过环境变量关闭以略微降低开销。
        self._enable_stage_timing = os.getenv("MC_STAGE_TIMING", "1") == "1"
        if self._capture_only:
            self._disable_ocr = True
        if self._disable_ocr:
            self._disable_uid = True
            self._disable_uid_recheck = True
        if self._disable_ocr:
            self._disable_panel_ocr = True
        if self._capture_only:
            self._disable_strategy = True
            self._disable_db = True
        self._detector_conf = float(os.getenv("MC_DETECTOR_CONF", "0.35"))
        self._use_gpu = use_gpu
        self._db_path = db_path
        self._db_write_key = db_write_key or generate_db_write_key()
        self._Session = None
        env_model_path = os.getenv("MC_YOLO_MODEL_PATH")
        if model_path or env_model_path:
            raw_model_path = model_path or env_model_path
        else:
            onnx_model_path = resource_path("models/yolov8_custom.onnx")
            raw_model_path = onnx_model_path if os.path.exists(onnx_model_path) else "models/yolov8_custom.pt"
        self._detector_model_path = resource_path(raw_model_path)
        # 设置采集帧率为 5fps，足以覆盖 UI 变化，同时极大降低 CPU/GPU 占用
        self.capture = CaptureWorker(proc_name=proc_name, fps=10)
        self.ocr = None
        self._ocr_initialized = False
        self._disable_detector_due_to_runtime_conflict = False
        #// 注意：Detector 的启用与否会影响场景判定和面板定位的准确性，尤其是在 UI 元素位置和颜色可能发生变化的情况下。禁用后将完全依赖预设的像素点匹配进行场景判定，这在某些情况下可能不够鲁棒。
        self.detector = None
        self._detector_initialized = False
        self.freq_model = FrequencyModel()
        self.bayes = BayesianUpdater()
        self.action_advisor = ActionStrategyAdvisor()
        self.strategy_config_path = writable_data_path(strategy_config_path)
        self.panel_layout_path = writable_data_path(panel_layout_path)
        self.strategy_reload_interval = 2.0
        self._next_strategy_reload_at = 0.0
        self._strategy_config_mtime = self._get_strategy_config_mtime()
        self._strategy_config_errors: List[str] = []
        self._strategy_config_used_default = False
        initial_cfg = load_strategy_priority_profile_with_meta(self.strategy_config_path)
        self.strategy_profile = initial_cfg.profile
        self._strategy_config_errors = initial_cfg.errors
        self._strategy_config_used_default = initial_cfg.used_default
        self._strategy_character_overrides: Dict[str, str] = {}
        self.observation_extractor = ObservationExtractor(
            echo_dictionary_path=writable_data_path(echo_dictionary_path),
            substat_values_path=writable_data_path(substat_values_path),
        )
        self.debug_dumper = RecognitionDebugDumper(base_dir="outputs/recognition")
        self.on_result = on_result  # UI callback
        self._tick_count = 0
        self.game_day_reset_hour = game_day_reset_hour

        self._uid_locked = False
        self._uid_retry_steps = [3, 5, 10]
        self._uid_retry_idx = 0
        self._next_uid_retry_at = 0.0
        try:
            self._uid_lock_confirmations = max(1, int(os.getenv("MC_UID_LOCK_CONFIRMATIONS", "2")))
        except ValueError:
            self._uid_lock_confirmations = 2
        self._uid_recheck_interval = 3.0
        self._uid_candidate_interval = float(os.getenv("MC_UID_CANDIDATE_INTERVAL", "0.6"))
        self._next_uid_recheck_at = 0.0
        self._last_uid = None
        self._uid_consistent_count = 0
        self._uid_debug_latest: Dict[str, Any] = {}
        self._uid_debug_print_interval = 1.0
        self._last_uid_debug_print_at = 0.0

        self._current_account_id: Optional[int] = None
        self._current_uid: Optional[str] = None
        self._current_client_started_at: Optional[datetime.datetime] = None
        self._current_client_pid: Optional[int] = None
        self._game_process_snapshot: Optional[GameProcessSnapshot] = None
        self._game_process_snapshot_at: float = 0.0
        self._just_restarted_client = False
        self._login_open_count = 0
        self._restart_open_count = 0

        self._active_session_by_account: Dict[int, Dict[str, Any]] = {}

        self._last_scene_signature: Optional[int] = None
        self._last_detection_out: Optional[Dict[str, Any]] = None
        self._last_echo_view: Optional[Dict[str, Any]] = None
        self._last_uid_ocr_at: float = 0.0
        self._last_uid_signature: Optional[int] = None
        self._last_uid_value: Optional[str] = None
        self._last_uid_debug_cache: Dict[str, Any] = {}
        self._last_panel_ocr_at: float = 0.0
        self._last_panel_ocr_signature: Optional[int] = None
        self._last_panel_ocr_result: Optional[Dict[str, Any]] = None
        self._last_echo_log_at: float = 0.0
        self._uid_crop_box = get_uid_crop_box()
        self._active_echo_context: Optional[ActiveEchoContext] = None
        self._echo_context_mismatch_confirmations = 2
        self._echo_context_match_threshold = 0.75
        self._echo_context_replace_threshold = 0.4
        self.panel_layout = self._load_panel_layout() # 加载面板布局配置，包含场景匹配和 OCR 区域定义
        print(
            "[Pipeline] flags"
            f" capture_only={self._capture_only}"
            f" disable_result_reuse={self._disable_result_reuse}"
            f" minimal_payload={self._minimal_payload}"
            f" uid_ocr_interval={self._uid_ocr_interval}"
            f" uid_candidate_interval={self._uid_candidate_interval}"
            f" uid_lock_confirmations={self._uid_lock_confirmations}"
            f" uid_crop_box={self._uid_crop_box}"
            f" panel_ocr_interval={self._panel_ocr_interval}"
            f" disable_ocr={self._disable_ocr}"
            f" disable_uid={self._disable_uid}"
            f" disable_uid_recheck={self._disable_uid_recheck}"
            f" disable_panel_ocr={self._disable_panel_ocr}"
            f" disable_strategy={self._disable_strategy}"
            f" disable_db={self._disable_db}"
            f" disable_detector_runtime_conflict={self._disable_detector_due_to_runtime_conflict}"
            f" stage_timing={self._enable_stage_timing}"
        )
        # 运行时阅读提示：
        # `tick()` 主循环状态依次为：
        # 1) 等待游戏进程
        # 2) UID 识别与锁定
        # 3) 场景判定/检测 + OCR + 解析
        # 4) 观测抽取 + 策略建议 + 入库

 
    @property
    def Session(self):
        if self._Session is None:
            self._Session = init_db(self._db_path, write_key=self._db_write_key)
        return self._Session

    def _ensure_ocr(self):
        if self._disable_ocr:
            return None
        if not self._ocr_initialized:
            from src.ocr import OCR

            self.ocr = OCR(use_gpu=self._use_gpu)
            self._ocr_initialized = True
            if self.ocr is not None:
                print(
                    "[Pipeline] OCR status"
                    f" backend={getattr(self.ocr, 'backend', 'unknown')}"
                    f" requested_gpu={getattr(self.ocr, 'requested_gpu', False)}"
                    f" use_gpu={getattr(self.ocr, 'use_gpu', False)}"
                    f" disabled_reason={getattr(self.ocr, 'disabled_reason', None)}"
                )
            self._disable_detector_due_to_runtime_conflict = (
                os.name == "nt"
                and self.ocr is not None
                and getattr(self.ocr, "backend", None) == "paddle-gpu"
                and os.getenv("MC_ALLOW_TORCH_WITH_PADDLE_GPU", "0") != "1"
            )
            if self._disable_detector_due_to_runtime_conflict:
                print(
                    "[Pipeline] Detector disabled:"
                    " PaddleOCR GPU and Torch cannot share one Windows process;"
                    " using pixel scene matching"
                )
        return self.ocr

    def _ensure_detector(self):
        if self._detector_initialized:
            return self.detector
        if not self._disable_ocr:
            self._ensure_ocr()
        if (
            self._disable_detector_due_to_runtime_conflict
            or not self._detector_model_path
            or not os.path.exists(self._detector_model_path)
        ):
            self._detector_initialized = True
            return self.detector

        print(f"[Pipeline] Detector loading model={self._detector_model_path} device=cuda")
        load_t0 = time.perf_counter()
        from src.detector import Detector

        self.detector = Detector(model_path=self._detector_model_path, device="cuda")
        self._detector_initialized = True
        if self.detector is not None:
            print(
                "[Pipeline] Detector status"
                f" ready={self.detector.ready}"
                f" model={self._detector_model_path}"
                f" conf={self._detector_conf}"
                f" load_ms={(time.perf_counter() - load_t0) * 1000:.1f}"
            )
        return self.detector

    @staticmethod
    def _default_panel_layout() -> Dict[str, Any]:
        return {
            "ocr_regions": {
                "echo_panel": {
                    "panel_bbox": [0.70, 0.08, 0.99, 0.74],
                },
                "enhance_panel": {
                    "panel_bbox": [0.70, 0.08, 0.99, 0.74],
                },
            },
        }
    #                     {"name": "substats", "rect": [0.74, 0.26, 0.99, 0.40]},
    #                     {"name": "set_effect", "rect": [0.74, 0.52, 0.99, 0.72]},
    #                 ],
    #             },
    #         },
    #     }

    def _load_panel_layout(self) -> Dict[str, Any]:
        default = self._default_panel_layout()
        try:
            if not os.path.exists(self.panel_layout_path):
                return default
            with open(self.panel_layout_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return default
            return payload
        except Exception:
            return default

    @staticmethod
    def _point_rgb(frame, x_ratio: float, y_ratio: float) -> Optional[List[int]]:
        try:
            h, w = frame.shape[:2]
            x = int(max(0, min(w - 1, round(w * x_ratio))))
            y = int(max(0, min(h - 1, round(h * y_ratio))))
            b, g, r = frame[y, x].tolist()
            return [int(r), int(g), int(b)]
        except Exception:
            return None

    
    def _get_scene_match_display_mode(self) -> str:
        try:
            hwnd = getattr(self.capture, "hwnd", None)
            borderless = is_window_borderless_fullscreen(hwnd) if hwnd else None
            if borderless is True:
                return "borderless"
            if borderless is False:
                return "windowed"
        except Exception:
            pass
        return "unknown"

    # 从配置中获取 YOLO 检测器的类别名称列表，如果配置无效则返回默认类别列表，确保检测器能够正确映射类别索引到名称，同时提供了灵活的配置方式以适应不同的检测需求。
    def _get_yolo_class_names(self) -> List[str]:
        try:
            detector_cfg = self.panel_layout.get("scene_detector", {}) if isinstance(self.panel_layout, dict) else {}
            names = detector_cfg.get("class_names", []) if isinstance(detector_cfg, dict) else []
            if isinstance(names, list) and names:
                return [str(name) for name in names]
        except Exception:
            pass
        return list(self._DEFAULT_YOLO_CLASSES)

    # //
    def _detect_scene_by_yolo(self, frame) -> Dict[str, Any]:
        # 判断当前显示模式（窗口化或无边框全屏），以选择对应的场景匹配配置和调试输出标签
        display_mode = self._get_scene_match_display_mode()
        detector = self._ensure_detector()
        # 如果显示器不存在则直接返回默认结果，避免后续访问 detector 属性导致的崩溃，同时在 source 字段标记检测结果来源于哪个模块，方便调试和分析
        if detector is None or not detector.ready:
            return {"scene": "unknown", "ui_mode": None, "bbox": None, "points": [], "display_mode": display_mode, "source": "none"}
        # 根据配置动态调整检测器的启用状态和置信度阈值，允许用户通过配置文件控制检测器的行为，以适应不同的性能需求和使用场景。
        detector_cfg = self.panel_layout.get("scene_detector", {}) if isinstance(self.panel_layout, dict) else {}
       # 确定是否启用YOLO检测器，如果配置项不是一个字典或者没有明确的 "enabled" 键，则默认启用检测器。只有当 "enabled" 键存在且值为 False 时才会禁用检测器，这样可以避免因配置错误导致的功能不可用，同时提供了灵活的配置方式。
        enabled = True if not isinstance(detector_cfg, dict) else bool(detector_cfg.get("enabled", True))
        if not enabled:
            return {"scene": "unknown", "ui_mode": None, "bbox": None, "points": [], "display_mode": display_mode, "source": "yolo-disabled"}
        # 获取 YOLO 检测器的类别名称列表，如果配置无效则返回默认类别列表，确保检测器能够正确映射类别索引到名称，同时提供了灵活的配置方式以适应不同的检测需求。
        class_names = self._get_yolo_class_names()
        conf = self._detector_conf
        #获得YOLO置信度
        if isinstance(detector_cfg, dict):
            conf = float(detector_cfg.get("confidence", conf))
        # 进行 YOLO 检测，获取原始检测结果列表，每个检测结果包含类别索引、边界框坐标和置信度等信息。根据配置动态调整检测器的行为，以适应不同的性能需求和使用场景。
        raw_detections = detector.predict(frame, conf=conf)
        candidates: List[Dict[str, Any]] = []
        for det in raw_detections:
            cls_idx = int(det.get("cls", -1))
            if cls_idx < 0 or cls_idx >= len(class_names):
                continue
            scene_name = class_names[cls_idx]
            bbox = [int(round(v)) for v in det.get("bbox", [])[:4]]
            if len(bbox) != 4:
                continue
            candidates.append(
                {
                    "scene": scene_name,
                    "ui_mode": scene_name,
                    "bbox": bbox,
                    "conf": float(det.get("conf", 0.0)),
                    "cls": cls_idx,
                }
            )
        # 如果没有检测到任何候选场景，则返回默认结果，避免后续访问 candidates[0] 导致的 IndexError，同时在 source 字段标记检测结果来源于哪个模块，方便调试和分析
        if not candidates:
            return {"scene": "unknown", "ui_mode": None, "bbox": None, "points": [], "display_mode": display_mode, "source": "yolo"}
        # 对检测到的候选场景列表按照置信度进行排序，选择置信度最高的作为最终的场景判定结果，提升准确率和适应动态 UI 的能力，同时保留其他候选场景的信息以供调试和分析。
        candidates.sort(key=lambda item: item.get("conf", 0.0), reverse=True)
        best = candidates[0]
        return {
            "scene": best["scene"],
            "ui_mode": best["ui_mode"],
            "bbox": best["bbox"],
            "points": candidates,
            "display_mode": display_mode,
            "source": "yolo",
        }

    @staticmethod
    def _region_bbox_within_panel(panel_bbox: List[int], panel_rect: List[float], region_rect: List[float]) -> List[int]:
        panel_x1, panel_y1, panel_x2, panel_y2 = panel_bbox
        panel_w = max(1, panel_x2 - panel_x1)
        panel_h = max(1, panel_y2 - panel_y1)

        ref_x1, ref_y1, ref_x2, ref_y2 = [float(v) for v in panel_rect]
        ref_w = max(1e-6, ref_x2 - ref_x1)
        ref_h = max(1e-6, ref_y2 - ref_y1)
        rx1 = (float(region_rect[0]) - ref_x1) / ref_w
        ry1 = (float(region_rect[1]) - ref_y1) / ref_h
        rx2 = (float(region_rect[2]) - ref_x1) / ref_w
        ry2 = (float(region_rect[3]) - ref_y1) / ref_h

        x1 = int(round(panel_x1 + rx1 * panel_w))
        y1 = int(round(panel_y1 + ry1 * panel_h))
        x2 = int(round(panel_x1 + rx2 * panel_w))
        y2 = int(round(panel_y1 + ry2 * panel_h))
        return [x1, y1, max(x1 + 1, x2), max(y1 + 1, y2)]

    # 场景匹配：根据预设的屏幕坐标点 RGB 颜色判断当前画面属于哪个场景（如声骸面板、强化面板、调律面板等），以决定后续 OCR 的裁剪区域和解析逻辑。
    # def _match_scene_by_pixels(self, frame) -> Dict[str, Any]:
    #     matcher = self.panel_layout.get("scene_match", {}) if isinstance(self.panel_layout, dict) else {}
    #     display_mode = self._get_scene_match_display_mode()
    #     # 如果场景匹配被禁用，直接返回默认结果
    #     if not matcher.get("enabled", True):
    #         return {"scene": "unknown", "ui_mode": None, "points": [], "display_mode": display_mode, "source": "pixel-disabled"}
    #     # 安全读取配置项，避免类型错误导致的崩溃
    #     modes = matcher.get("modes", {}) if isinstance(matcher, dict) else {}
    #     scenes = {}
    #     if isinstance(modes, dict):
    #         mode_cfg = modes.get(display_mode, {}) if display_mode in modes else {}
    #         if isinstance(mode_cfg, dict):
    #             scenes = mode_cfg.get("scenes", {})
    #     if not isinstance(scenes, dict) or not scenes:
    #         scenes = matcher.get("scenes", {})
    #     if not isinstance(scenes, dict):
    #         return {"scene": "unknown", "ui_mode": None, "points": [], "display_mode": display_mode, "source": "pixel"}
    #     # 匹配模式：严格匹配（exact=True）要求 RGB 完全一致，宽松匹配（exact=False）允许一定容差（tolerance）内的差异。
    #     exact = bool(matcher.get("exact", True))
    #     tolerance = int(matcher.get("tolerance", 0))
    #     debug_points: List[Dict[str, Any]] = []

    #     # 遍历场景配置，检查每个场景定义的关键点颜色是否匹配当前画面。匹配成功则返回对应的场景名称和 UI 模式。
    #     for scene_name, scene_cfg in scenes.items():
    #         points = scene_cfg.get("points", []) if isinstance(scene_cfg, dict) else []
    #         if not points or len(points) < 3:
    #             continue

    #         ok = True
    #         # 对每个关键点，采样当前画面对应位置的 RGB 颜色，与配置中的目标 RGB 进行比较。根据匹配模式判断是否满足条件。
    #         for p in points[:3]:
    #             target_rgb = p.get("rgb", [])
    #             if len(target_rgb) != 3:
    #                 ok = False
    #                 break
    #             # 采样当前画面对应位置的 RGB 颜色
    #             sampled = self._point_rgb(frame, float(p.get("x", 0)), float(p.get("y", 0)))
    #             if sampled is None:
    #                 ok = False
    #                 break
    #             # 计算采样颜色与目标颜色的差异
    #             diffs = [abs(int(sampled[i]) - int(target_rgb[i])) for i in range(3)]
    #             matched = False
    #             if exact:
    #                 matched = not any(d != 0 for d in diffs)
    #                 if not matched:
    #                     ok = False
    #             else:
    #                 matched = not any(d > tolerance for d in diffs)
    #                 if not matched:
    #                     ok = False
    #             debug_points.append(
    #                 {
    #                     "scene": scene_name,
    #                     "ui_mode": scene_cfg.get("ui_mode"),
    #                     "x_ratio": float(p.get("x", 0)),
    #                     "y_ratio": float(p.get("y", 0)),
    #                     "target_rgb": [int(v) for v in target_rgb],
    #                     "sampled_rgb": [int(v) for v in sampled],
    #                     "diffs": diffs,
    #                     "matched": matched,
    #                 }
    #             )
    #             if not matched:
    #                 break
    #         if ok:
    #             return {
    #                 "scene": scene_name,
    #                 "ui_mode": scene_cfg.get("ui_mode"),
    #                 "points": debug_points,
    #                 "display_mode": display_mode,
    #                 "source": "pixel",
    #             }
    #     return {"scene": "unknown", "ui_mode": None, "points": debug_points, "display_mode": display_mode, "source": "pixel"}

    @staticmethod
    def _rect_ratio_to_bbox(frame, rect: List[float]) -> List[int]:
        h, w = frame.shape[:2]
        x1 = int(max(0, min(w - 1, round(w * float(rect[0])))))
        y1 = int(max(0, min(h - 1, round(h * float(rect[1])))))
        x2 = int(max(x1 + 1, min(w, round(w * float(rect[2])))))
        y2 = int(max(y1 + 1, min(h, round(h * float(rect[3])))))
        return [x1, y1, x2, y2]

    def _ocr_scene_regions(self, frame, scene: str, panel_bbox_override: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
        ocr = self._ensure_ocr()
        if ocr is None:
            return None
        ocr_regions = self.panel_layout.get("ocr_regions", {}) if isinstance(self.panel_layout, dict) else {}
        scene_cfg = ocr_regions.get(scene, {}) if isinstance(ocr_regions, dict) else {}
        if not isinstance(scene_cfg, dict):
            return None

        panel_rect = scene_cfg.get("panel_bbox")
        if not panel_rect:
            return None

        if panel_bbox_override and len(panel_bbox_override) == 4:
            panel_bbox = [int(v) for v in panel_bbox_override]
        else:
            panel_bbox = self._rect_ratio_to_bbox(frame, panel_rect)
        panel_crop = crop_bbox(frame, panel_bbox)
        panel_signature = self._image_signature(panel_crop)
        now = time.time()
        if (
            panel_signature is not None
            and panel_signature == self._last_panel_ocr_signature
            and (now - self._last_panel_ocr_at) < self._panel_ocr_interval
        ):
            return self._last_panel_ocr_result

        # 使用全文 OCR 空间坐标分析法代替固定切片
        ocr_input = upscale_if_small(panel_crop, min_height=56)
        ocr_res = ocr.recognize_with_boxes(ocr_input)
        if not ocr_res:
            ocr_res = ocr.recognize_with_boxes(enhance_for_ocr(panel_crop))

        texts: List[str] = group_ocr_texts_by_y(ocr_res, y_threshold=15)

        if not texts:
            self._last_panel_ocr_signature = panel_signature
            self._last_panel_ocr_at = now
            self._last_panel_ocr_result = None
            return None

        out = {
            "bbox": panel_bbox,
            "raw_texts": texts,
            "ui_mode": scene,
        }
        self._last_panel_ocr_signature = panel_signature
        self._last_panel_ocr_at = now
        self._last_panel_ocr_result = out
        return out

    def _scene_panel_signature(self, frame, scene: str, panel_bbox_override: Optional[List[int]] = None) -> Optional[int]:
        """对已命中的场景面板生成轻量签名，用于复用上次 OCR 结果。"""
        try:
            ocr_regions = self.panel_layout.get("ocr_regions", {}) if isinstance(self.panel_layout, dict) else {}
            scene_cfg = ocr_regions.get(scene, {}) if isinstance(ocr_regions, dict) else {}
            if not isinstance(scene_cfg, dict):
                return None
            panel_rect = scene_cfg.get("panel_bbox")
            if not panel_rect:
                return None

            if panel_bbox_override and len(panel_bbox_override) == 4:
                panel_bbox = [int(v) for v in panel_bbox_override]
            else:
                panel_bbox = self._rect_ratio_to_bbox(frame, panel_rect)
            panel_crop = crop_bbox(frame, panel_bbox)
            if panel_crop.size == 0:
                return None

            step_y = max(1, panel_crop.shape[0] // 24)
            step_x = max(1, panel_crop.shape[1] // 24)
            small = panel_crop[::step_y, ::step_x]
            return hash((
                scene,
                tuple(panel_crop.shape),
                int(small[..., 0].mean()),
                int(small[..., 1].mean()),
                int(small[..., 2].mean()),
                int(small.std()),
            ))
        except Exception:
            return None

    @staticmethod
    def _image_signature(image, grid: int = 24) -> Optional[int]:
        try:
            if image is None or getattr(image, "size", 0) == 0:
                return None
            step_y = max(1, image.shape[0] // grid)
            step_x = max(1, image.shape[1] // grid)
            small = image[::step_y, ::step_x]
            if image.ndim == 3:
                return hash((
                    tuple(image.shape),
                    int(small[..., 0].mean()),
                    int(small[..., 1].mean()),
                    int(small[..., 2].mean()),
                    int(small.std()),
                ))
            return hash((tuple(image.shape), int(small.mean()), int(small.std())))
        except Exception:
            return None

    def _reuse_last_detection_out(self, frame, capture_fps: float) -> Dict[str, Any]:
        out = dict(self._last_detection_out or {})
        out["status"] = "running"
        out["uid"] = self._current_uid
        out["account_id"] = self._current_account_id
        out["client_pid"] = self._current_client_pid
        out["client_started_at"] = self._current_client_started_at.isoformat() if self._current_client_started_at else None
        out["strategy_config"] = self._strategy_config_status()
        out["frame_shape"] = list(frame.shape)
        out["latency_ms"] = 0
        out["tick"] = self._tick_count
        out["capture_fps"] = round(capture_fps, 1)
        return out

    def _build_base_out(self, status: str, frame, capture_fps: float, detections: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        out = {
            "status": status,
            "uid": self._current_uid,
            "uid_crop_box": list(self._uid_crop_box),
            "account_id": self._current_account_id,
            "client_pid": self._current_client_pid,
            "client_started_at": self._current_client_started_at.isoformat() if self._current_client_started_at else None,
            "strategy_config": self._strategy_config_status(),
            "frame_shape": list(frame.shape),
            "detections": detections or [],
            "latency_ms": 0,
            "tick": self._tick_count,
            "capture_fps": round(capture_fps, 1),
        }
        if self._minimal_payload:
            out.pop("strategy_config", None)
            out.pop("frame_shape", None)
            out["detections"] = []
        return out

    @staticmethod
    def _format_stat_value(substat: Dict[str, Any]) -> str:
        value = substat.get("value")
        if value is None:
            return ""
        suffix = "%" if substat.get("is_pct") else ""
        return f"{value}{suffix}"

    #这里是将 OCR 识别出的文本和检测到的场景信息构建成一个统一的视图结构，包含了声骸的等级、主属性、副属性、套装效果等信息，以及根据策略建议生成的推荐操作和理由。这部分代码的设计目的是为了将复杂的 OCR 结果和场景信息抽象成一个更易于理解和使用的格式，方便后续的策略分析和 UI 展示。
    def _build_echo_view(
        self,
        obs: EchoObservation,
        strategy_advice: Optional[Dict[str, Any]] = None,
        echo_instance_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        advice = strategy_advice or {}
        substat_probabilities = advice.get("substat_probabilities", {})
        if not isinstance(substat_probabilities, dict):
            substat_probabilities = {}
        substat_posterior = advice.get("substat_posterior", {})
        if not isinstance(substat_posterior, dict):
            substat_posterior = {}

        substats_by_slot = {idx + 1: sub for idx, sub in enumerate(obs.substats or [])}
        slots: List[Dict[str, Any]] = []
        for slot in obs.slot_states or []:
            slot_index = int(slot.get("slot_index", len(slots) + 1))
            slot_name = slot.get("name")
            slot_value = slot.get("value")
            slot_is_pct = slot.get("is_pct")
            slot_quality = slot.get("quality")

            # 兼容旧结构：当 slot_states 不带 name/value 时再回退到顺序映射
            if slot_name is None and slot_value is None and slot_index in substats_by_slot:
                legacy_sub = substats_by_slot.get(slot_index)
                slot_name = legacy_sub.get("name") if legacy_sub else None
                slot_value = legacy_sub.get("value") if legacy_sub else None
                slot_is_pct = legacy_sub.get("is_pct") if legacy_sub else None
                slot_quality = legacy_sub.get("quality") if legacy_sub else None

            value_text = ""
            if slot_value is not None:
                suffix = "%" if slot_is_pct else ""
                value_text = f"{slot_value}{suffix}"

            probability = None
            probability_text = ""
            if slot_name:
                raw_probability = substat_probabilities.get(str(slot_name).strip())
                try:
                    if raw_probability is not None:
                        probability = float(raw_probability)
                        probability_text = f"{probability * 100:.1f}%"
                except (TypeError, ValueError):
                    probability = None
                    probability_text = ""

            slots.append(
                {
                    "index": slot_index,
                    "status": slot.get("status", ""),
                    "text": slot.get("text", ""),
                    "name": slot_name,
                    "value": slot_value,
                    "is_pct": slot_is_pct,
                    "value_text": value_text,
                    "quality": slot_quality,
                    "probability": probability,
                    "probability_text": probability_text,
                }
            )
        while len(slots) < 5:
            slot_index = len(slots) + 1
            threshold = self._slot_threshold(slot_index)
            slots.append(
                {
                    "index": slot_index,
                    "status": "locked_by_level",
                    "text": f"强化至+{threshold}可调谐",
                    "name": None,
                    "value": None,
                    "is_pct": None,
                    "value_text": "",
                    "quality": None,
                    "probability": None,
                    "probability_text": "",
                }
            )

        recommended_action = advice.get("recommended_action", "single")
        if recommended_action == "multi":
            advice_text = "多开"
        elif recommended_action == "disabled":
            advice_text = "策略已禁用"
        elif recommended_action == "switch_echo":
            advice_text = "换声骸"
        elif recommended_action == "park_echo":
            advice_text = "暂存换胚"
        elif recommended_action == "continue_echo":
            advice_text = "继续当前"
        elif recommended_action == "finished":
            advice_text = "已满词条"
        elif recommended_action == "restart_client":
            advice_text = "重启客户端"
        elif recommended_action == "stop":
            advice_text = "停止强化"
        else:
            advice_text = "单开"
        reason = advice.get("reason", "")
        cost_analysis = advice.get("cost_analysis", {})
        if not isinstance(cost_analysis, dict):
            cost_analysis = {}
        perfect_cost_main_stats = advice.get("perfect_cost_main_stats", {})
        perfect_consonant = advice.get("perfect_consonant", {})
        if not isinstance(perfect_cost_main_stats, dict):
            perfect_cost_main_stats = {}
        if not isinstance(perfect_consonant, dict):
            perfect_consonant = {}

        return {
            "echo_instance_id": echo_instance_id or "",
            "echo_name": obs.echo_name,
            "level": obs.level,
            "level_text": f"+{obs.level}" if obs.level is not None else "+?",
            "cost": obs.cost,
            "set_name": obs.set_name,
            "main_stat": obs.main_stat,
            "equipment": obs.equipment,
            "ui_mode": obs.ui_mode,
            "is_locked": obs.is_locked,
            "slots": slots[:5],
            "substat_probabilities": substat_probabilities,
            "substat_posterior": substat_posterior,
            "strategy_role": {
                "set_name": obs.set_name,
                "selected": advice.get("strategy_role_selected"),
                "available": advice.get("strategy_role_available", []) or [],
                "source": advice.get("strategy_role_source", "default"),
            },
            "perfect_recommendation": {
                "cost_main_stats": perfect_cost_main_stats,
                "consonant": perfect_consonant,
                "source": advice.get("perfect_source", "default"),
            },
            "advice": {
                "text": advice_text,
                "recommended_action": recommended_action,
                "reason": reason,
                "single_score": advice.get("single_score", 0.0),
                "multi_score": advice.get("multi_score", 0.0),
                "single_samples": advice.get("single_samples", 0),
                "multi_samples": advice.get("multi_samples", 0),
                "cost_analysis": cost_analysis,
            },
        }

    @staticmethod
    def _compact_detection_for_ui(result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "bbox": result.get("bbox"),
            "conf": result.get("conf"),
            "scene": result.get("scene"),
            "ui_mode": result.get("ui_mode"),
            "echo": result.get("echo"),
        }

    @staticmethod
    def _is_usable_echo_view(echo: Any) -> bool:
        if not isinstance(echo, dict):
            return False
        identity_values = (
            echo.get("echo_name"),
            echo.get("set_name"),
            echo.get("main_stat"),
            echo.get("equipment"),
        )
        if any(str(value or "").strip() for value in identity_values):
            return True
        if echo.get("cost") is not None:
            return True
        for slot in echo.get("slots") or []:
            if isinstance(slot, dict) and str(slot.get("name") or "").strip():
                return True
        return False

    @staticmethod
    def _select_echo_for_output(
        public_results: List[Dict[str, Any]],
        last_echo_view: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for result in public_results:
            echo = result.get("echo")
            if PipelineRunner._is_usable_echo_view(echo):
                return echo

        if PipelineRunner._is_usable_echo_view(last_echo_view):
            return last_echo_view
        return None

    @staticmethod
    def _identity_view(obs: EchoObservation) -> Dict[str, Any]:
        candidate = obs.identity_candidate if isinstance(obs.identity_candidate, dict) else None
        if candidate:
            return candidate
        return {
            "echo_name": obs.echo_name,
            "cost": obs.cost,
            "set_name": obs.set_name,
            "main_stat": obs.main_stat,
            "equipment": obs.equipment,
        }

    @classmethod
    def _echo_identity_signature(cls, obs: EchoObservation) -> Tuple[str, ...]:
        identity = cls._identity_view(obs)
        substat_parts = []
        for sub in (obs.substats or [])[:5]:
            name = str(sub.get("name") or "").strip()
            if not name:
                continue
            value = sub.get("value")
            is_pct = "pct" if sub.get("is_pct") else "flat"
            substat_parts.append(f"{name}:{value}:{is_pct}")
        return (
            str(identity.get("echo_name") or "").strip(),
            str(identity.get("cost") or ""),
            str(identity.get("set_name") or "").strip(),
            str(identity.get("main_stat") or "").strip(),
            str(identity.get("equipment") or "").strip(),
            "|".join(substat_parts),
        )

    @classmethod
    def _has_echo_identity(cls, obs: EchoObservation) -> bool:
        identity = cls._identity_view(obs)
        return any(
            str(value or "").strip()
            for value in (
                identity.get("echo_name"),
                identity.get("set_name"),
                identity.get("main_stat"),
                identity.get("equipment"),
            )
        ) or identity.get("cost") is not None or bool(obs.substats)

    @classmethod
    def _has_complete_echo_identity(cls, obs: EchoObservation) -> bool:
        identity = cls._identity_view(obs)
        return all(
            str(identity.get(field_name) or "").strip()
            for field_name in ("echo_name", "set_name", "main_stat")
        ) and identity.get("cost") is not None

    @classmethod
    def _clone_observation_with_identity(
        cls,
        target: EchoObservation,
        source: EchoObservation,
    ) -> EchoObservation:
        source_identity = cls._identity_view(source)
        return replace(
            target,
            level=target.level if target.level is not None else source.level,
            echo_name=source.echo_name,
            cost=source.cost,
            set_name=source.set_name,
            main_stat=source.main_stat,
            equipment=source.equipment,
            identity_candidate=dict(source_identity),
        )

    @classmethod
    def _has_hard_identity_conflict(cls, active: EchoObservation, candidate: EchoObservation) -> bool:
        active_identity = cls._identity_view(active)
        candidate_identity = cls._identity_view(candidate)
        for field_name in ("echo_name", "cost", "set_name", "main_stat", "equipment"):
            active_value = active_identity.get(field_name)
            candidate_value = candidate_identity.get(field_name)
            if active_value in (None, "") or candidate_value in (None, ""):
                continue
            if active_value != candidate_value:
                return True
        return False

    @classmethod
    def _same_echo_name(cls, active: EchoObservation, candidate: EchoObservation) -> bool:
        active_name = str(cls._identity_view(active).get("echo_name") or "").strip()
        candidate_name = str(cls._identity_view(candidate).get("echo_name") or "").strip()
        return bool(active_name and candidate_name and active_name == candidate_name)

    @staticmethod
    def _substat_name_sequence(obs: EchoObservation) -> List[str]:
        return [
            str(sub.get("name") or "").strip()
            for sub in (obs.substats or [])[:5]
            if str(sub.get("name") or "").strip()
        ]

    @classmethod
    def _echo_context_match_score(cls, active: EchoObservation, candidate: EchoObservation) -> float:
        if cls._has_hard_identity_conflict(active, candidate):
            return 0.0

        active_identity = cls._identity_view(active)
        candidate_identity = cls._identity_view(candidate)
        score = 0.0
        total = 0.0

        checks = (
            ("echo_name", 2.0),
            ("cost", 2.0),
            ("set_name", 2.0),
            ("main_stat", 2.0),
            ("equipment", 1.0),
        )
        for field_name, weight in checks:
            active_value = active_identity.get(field_name)
            candidate_value = candidate_identity.get(field_name)
            if active_value in (None, "") or candidate_value in (None, ""):
                continue
            total += weight
            if active_value == candidate_value:
                score += weight

        active_substats = {
            str(sub.get("name") or "").strip()
            for sub in (active.substats or [])
            if str(sub.get("name") or "").strip()
        }
        candidate_substats = {
            str(sub.get("name") or "").strip()
            for sub in (candidate.substats or [])
            if str(sub.get("name") or "").strip()
        }
        if active_substats and candidate_substats:
            total += 2.0
            score += 2.0 * (len(active_substats & candidate_substats) / max(len(active_substats | candidate_substats), 1))

        active_seq = cls._substat_name_sequence(active)
        candidate_seq = cls._substat_name_sequence(candidate)
        if active_seq and candidate_seq:
            compared = min(len(active_seq), len(candidate_seq))
            if compared > 0:
                total += 1.0
                same_position = sum(1 for idx in range(compared) if active_seq[idx] == candidate_seq[idx])
                score += same_position / compared

        if active.level is not None and candidate.level is not None:
            total += 1.0
            if candidate.level >= active.level:
                score += 1.0

        if total <= 0:
            return 0.0
        return score / total

    def _create_active_echo_context(self, obs: EchoObservation, scene: str, now: float) -> EchoObservation:
        self._active_echo_context = ActiveEchoContext(
            local_id=make_uuid(),
            anchor_scene=scene,
            obs=obs,
            last_seen_at=now,
        )
        return obs

    def _resolve_active_echo_observation(
        self,
        obs: EchoObservation,
        scene: Optional[str],
        now: Optional[float] = None,
    ) -> EchoObservation:
        now = time.time() if now is None else now
        scene = scene or obs.ui_mode or ""
        is_detail_scene = scene == "echo_panel"
        has_identity = self._has_echo_identity(obs)
        has_complete_identity = self._has_complete_echo_identity(obs)

        if not has_identity:
            if self._active_echo_context is not None:
                self._active_echo_context.last_seen_at = now
                self._active_echo_context.pending_signature = None
                self._active_echo_context.pending_count = 0
                return self._active_echo_context.obs
            return obs

        if is_detail_scene:
            if has_complete_identity:
                return self._create_active_echo_context(obs, scene, now)
            if self._active_echo_context is not None:
                self._active_echo_context.last_seen_at = now
                return self._clone_observation_with_identity(obs, self._active_echo_context.obs)
            return obs

        context = self._active_echo_context
        if context is None:
            if has_identity:
                return self._create_active_echo_context(obs, scene, now)
            return obs

        if self._has_hard_identity_conflict(context.obs, obs) and self._same_echo_name(context.obs, obs):
            context.obs = self._clone_observation_with_identity(obs, context.obs)
            context.last_seen_at = now
            context.pending_signature = None
            context.pending_count = 0
            return context.obs

        score = self._echo_context_match_score(context.obs, obs)
        if score >= self._echo_context_match_threshold:
            context.obs = self._clone_observation_with_identity(obs, context.obs)
            context.last_seen_at = now
            context.pending_signature = None
            context.pending_count = 0
            return context.obs

        if score >= self._echo_context_replace_threshold:
            context.pending_signature = None
            context.pending_count = 0
            context.last_seen_at = now
            return context.obs

        signature = self._echo_identity_signature(obs)
        if signature == context.pending_signature:
            context.pending_count += 1
        else:
            context.pending_signature = signature
            context.pending_count = 1

        context.last_seen_at = now
        return context.obs

    def _log_stage_timing(self, timings: Dict[str, float]):
        if not self._enable_stage_timing:
            return
        parts = [f"{name}={value:.1f}ms" for name, value in timings.items()]
        #显示每个阶段的耗时，帮助分析性能瓶颈和优化效果
        # print(f"[Pipeline-Timing] {' '.join(parts)}")

    @property
    def echo_dictionary(self) -> Dict[str, List[str]]:
        return self.observation_extractor.echo_dictionary

    @echo_dictionary.setter
    def echo_dictionary(self, value: Dict[str, List[str]]):
        self.observation_extractor.echo_dictionary = value

    def _reload_echo_dictionary_if_needed(self, force: bool = False):
        self.observation_extractor.reload_echo_dictionary_if_needed(force=force)

    @staticmethod
    def _slot_threshold(slot_index: int) -> int:
        return ObservationExtractor.slot_threshold(slot_index)

    def _build_slot_states(
        self,
        level: Optional[int],
        activated_substats: List[Dict[str, Any]],
        ui_mode: str,
    ) -> List[Dict[str, Any]]:
        return self.observation_extractor.build_slot_states(
            level=level,
            activated_substats=activated_substats,
            ui_mode=ui_mode,
        )

    def _get_strategy_config_mtime(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.strategy_config_path)
        except OSError:
            return None

    def _reload_strategy_profile_if_needed(self, force: bool = False):
        if self._disable_strategy:
            return
        now = time.time()
        if not force and now < self._next_strategy_reload_at:
            return
        self._next_strategy_reload_at = now + self.strategy_reload_interval

        latest_mtime = self._get_strategy_config_mtime()
        if not force and latest_mtime == self._strategy_config_mtime:
            return

        cfg = load_strategy_priority_profile_with_meta(self.strategy_config_path)
        self.strategy_profile = cfg.profile
        self._strategy_config_errors = cfg.errors
        self._strategy_config_used_default = cfg.used_default
        self._strategy_config_mtime = latest_mtime

        # 配置热更新后移除失效的角色覆盖选择。
        for set_name, role_name in list(self._strategy_character_overrides.items()):
            available = self.strategy_profile.available_characters_for_set(set_name)
            if role_name not in available:
                self._strategy_character_overrides.pop(set_name, None)

    def set_strategy_character_for_set(self, set_name: str, role_name: str) -> bool:
        set_name = str(set_name or "").strip()
        role_name = str(role_name or "").strip()
        if not set_name or not role_name:
            return False

        available = self.strategy_profile.available_characters_for_set(set_name)
        if role_name not in available:
            return False

        self._strategy_character_overrides[set_name] = role_name
        # 切换策略后立即让下一帧重新计算，避免复用旧结果。
        self._last_scene_signature = None
        self._last_detection_out = None
        return True

    def _strategy_config_status(self) -> Dict[str, Any]:
        return {
            "path": self.strategy_config_path,
            "used_default": self._strategy_config_used_default,
            "errors": list(self._strategy_config_errors),
            "error_count": len(self._strategy_config_errors),
        }

    def _resolve_strategy_role(
        self,
        set_name: str,
        equipment: Optional[str] = None,
    ) -> tuple[Dict[str, float], str, Optional[str], List[str]]:
        override = self._strategy_character_overrides.get(set_name)
        equipment_name = str(equipment or "").strip() or None
        preferred = override or equipment_name
        weights, source, selected, available = self.strategy_profile.weights_for(
            set_name=set_name,
            character_name=preferred,
        )

        # 若覆盖角色失效（例如配置热更后被删除），自动清理。
        if override and override not in available:
            self._strategy_character_overrides.pop(set_name, None)
            if equipment_name and equipment_name in available:
                weights, source, selected, available = self.strategy_profile.weights_for(
                    set_name=set_name,
                    character_name=equipment_name,
                )

        return weights, source, selected, available

    def _resolve_perfect_recommendation(self, set_name: str, selected_role: Optional[str]) -> Dict[str, Any]:
        perfect_meta, perfect_source, _, _ = self.strategy_profile.perfect_for(
            set_name=set_name,
            character_name=selected_role,
        )
        if not isinstance(perfect_meta, dict):
            perfect_meta = {}
        cost_main_stats = perfect_meta.get("cost_main_stats", {})
        consonant = perfect_meta.get("consonant", {})
        if not isinstance(cost_main_stats, dict):
            cost_main_stats = {}
        if not isinstance(consonant, dict):
            consonant = {}
        return {
            "cost_main_stats": cost_main_stats,
            "consonant": consonant,
            "source": perfect_source,
        }

    def _estimate_action_advice(self, db_sess, account_id: int, obs: EchoObservation) -> Dict[str, Any]:
        tunable_slots_count = sum(
            1 for s in obs.slot_states if s.get("status") in ("current_tunable", "pending_tunable")
        )

        events = (
            db_sess.query(EchoSubstat)
            .filter(
                EchoSubstat.account_id == account_id,
                EchoSubstat.action_type.in_(["single", "multi"]),
            )
            .order_by(EchoSubstat.created_at.desc())
            .limit(500)
            .all()
        )

        event_records: List[Dict[str, Any]] = []
        for ev in events:
            if obs.cost is not None and ev.session is not None and ev.session.cost != obs.cost:
                continue
            event_records.append(
                {
                    "action_type": ev.action_type,
                    "value_tier": ev.value_tier,
                    "substat_name": ev.substat_name,
                    "substat_value": ev.substat_value,
                    "is_pct": (
                        float(ev.substat_value) < 20
                        if ev.substat_name in ("攻击", "防御") and ev.substat_value is not None
                        else None
                    ),
                    "is_historical_unknown": ev.is_historical_unknown,
                    "cost": ev.session.cost if ev.session else None,
                }
            )
        substat_counts = Counter(
            str(record.get("substat_name") or "").strip()
            for record in event_records
            if str(record.get("substat_name") or "").strip()
        )
        total_substat_samples = sum(substat_counts.values())
        substat_probabilities = {
            name: count / total_substat_samples
            for name, count in substat_counts.items()
            if total_substat_samples > 0
        }

        priority_weights, priority_source, selected_role, available_roles = self._resolve_strategy_role(
            obs.set_name,
            equipment=obs.equipment,
        )
        perfect_recommend = self._resolve_perfect_recommendation(obs.set_name, selected_role)
        cost_analysis = self.action_advisor.analyze_cost(
            obs.substats,
            perfect_consonant=perfect_recommend["consonant"],
            priority_weights=priority_weights,
            fallback=self.strategy_profile.fallback_weight,
        )

        if int(cost_analysis.get("remaining_slots", 0)) <= 0:
            substat_posterior = {
                "skipped": True,
                "reason": "已开满5个辅音，无剩余孔位可预测",
                "probabilities": {},
                "predictions": [],
                "samples": {},
                "blocked": [],
            }
        else:
            # “大数据”预测使用本地库中所有账号的可见辅音记录，样本越多越稳。
            posterior_events = (
                db_sess.query(EchoSubstat)
                .filter(EchoSubstat.action_type.in_(["single", "multi", "history"]))
                .order_by(EchoSubstat.created_at.desc())
                .limit(2000)
                .all()
            )
            posterior_records: List[Dict[str, Any]] = []
            for ev in posterior_events:
                session = ev.session
                posterior_records.append(
                    {
                        "substat_name": ev.substat_name,
                        "substat_value": ev.substat_value,
                        "is_pct": (
                            float(ev.substat_value) < 20
                            if ev.substat_name in ("攻击", "生命", "防御") and ev.substat_value is not None
                            else None
                        ),
                        "cost": session.cost if session else None,
                        "set_name": session.set_name if session else "",
                        "main_stat": session.main_stat if session else "",
                        "slot_index": ev.slot_index,
                    }
                )
            substat_posterior = SubstatPosteriorModel().fit(posterior_records).predict(
                {
                    "cost": obs.cost,
                    "set_name": obs.set_name,
                    "main_stat": obs.main_stat,
                    "existing_substats": obs.substats,
                },
                candidate_pool=self.observation_extractor.substat_values,
                top_n=8,
            )
            cost_analysis = self.action_advisor.analyze_cost(
                obs.substats,
                perfect_consonant=perfect_recommend["consonant"],
                priority_weights=priority_weights,
                substat_posterior=substat_posterior,
                fallback=self.strategy_profile.fallback_weight,
            )

        weight_hits = []
        for sub in obs.substats:
            name = str(sub.get("name", ""))
            if not name:
                continue
            weight_hits.append(
                {
                    "substat_name": name,
                    "weight": self.action_advisor._priority_weight(
                        {
                            "substat_name": name,
                            "substat_value": sub.get("value"),
                            "is_pct": sub.get("is_pct"),
                        },
                        priority_weights,
                        fallback=self.strategy_profile.fallback_weight,
                    ),
                }
            )
        advice = self.action_advisor.recommend(
            event_records,
            tunable_slots_count=tunable_slots_count,
            priority_weights=priority_weights,
        )
        recommended_action = advice.recommended_action
        reason = advice.reason
        if cost_analysis.get("recommended_action"):
            recommended_action = str(cost_analysis["recommended_action"])
            reason = str(cost_analysis.get("reason") or reason)
        return {
            "recommended_action": recommended_action,
            "reason": reason,
            "single_score": advice.single_score,
            "multi_score": advice.multi_score,
            "single_samples": advice.single_samples,
            "multi_samples": advice.multi_samples,
            "tunable_slots_count": tunable_slots_count,
            "priority_set": obs.set_name,
            "priority_source": priority_source,
            "priority_weight_hits": weight_hits,
            "strategy_role_selected": selected_role,
            "strategy_role_available": available_roles,
            "strategy_role_source": priority_source,
            "perfect_cost_main_stats": perfect_recommend["cost_main_stats"],
            "perfect_consonant": perfect_recommend["consonant"],
            "perfect_source": perfect_recommend["source"],
            "substat_probabilities": substat_probabilities,
            "substat_probability_samples": total_substat_samples,
            "substat_posterior": substat_posterior,
            "cost_analysis": cost_analysis,
        }

    def start_capture(self):
        self.capture.start()

    def stop(self):
        self.capture.stop()

    # 缓存游戏进程身份，避免每次 tick 都遍历全部进程。
    # PID 与 create_time 放在同一个快照中，后续登录/重启判断只读这一处状态。
    _GAME_PROCESS_SNAPSHOT_TTL: float = 3.0  # 秒

    def _scan_game_process_snapshot(self) -> Optional[GameProcessSnapshot]:
        now = time.time()
        best: Optional[GameProcessSnapshot] = None
        try:
            for proc in psutil.process_iter(["pid", "name", "create_time"]):
                name = proc.info.get("name")
                if not name or self.proc_name.lower() not in name.lower():
                    continue
                pid = proc.info.get("pid")
                create_time = proc.info.get("create_time")
                if not pid or not create_time:
                    continue
                started_at = datetime.datetime.fromtimestamp(create_time).replace(microsecond=0)
                snapshot = GameProcessSnapshot(pid=int(pid), started_at=started_at, captured_at=now)
                if best is None or snapshot.started_at < best.started_at:
                    best = snapshot
        except Exception:
            return None
        return best

    def _clear_uid_binding_after_process_change(self):
        self._uid_locked = False
        self._current_uid = None
        self._current_account_id = None
        self._current_client_started_at = None
        self._current_client_pid = None
        self._uid_consistent_count = 0
        self._last_uid = None
        self._next_uid_retry_at = 0.0
        self._uid_retry_idx = 0

    def _refresh_game_process_snapshot(self, force: bool = False) -> Optional[GameProcessSnapshot]:
        now = time.time()
        if (
            not force
            and (now - self._game_process_snapshot_at) < self._GAME_PROCESS_SNAPSHOT_TTL
        ):
            return self._game_process_snapshot

        previous = self._game_process_snapshot
        snapshot = self._scan_game_process_snapshot()
        self._game_process_snapshot = snapshot
        self._game_process_snapshot_at = now

        if previous and snapshot and (
            previous.pid != snapshot.pid or previous.started_at != snapshot.started_at
        ):
            print(
                "[Pipeline] game process changed"
                f" old_pid={previous.pid} old_started={previous.started_at}"
                f" new_pid={snapshot.pid} new_started={snapshot.started_at}"
            )
            self._clear_uid_binding_after_process_change()
        return snapshot

    def _is_game_running(self) -> bool:
        return self._refresh_game_process_snapshot() is not None

    def _get_game_process_snapshot(self) -> Optional[GameProcessSnapshot]:
        return self._refresh_game_process_snapshot(force=True)

    def _get_process_start_time(self) -> Optional[datetime.datetime]:
        snapshot = self._get_game_process_snapshot()
        return snapshot.started_at if snapshot else None

    def _extract_uid_from_frame(self, frame) -> Optional[str]:
        ocr = self._ensure_ocr()
        if ocr is None:
            self._uid_debug_latest = {
                "raw_texts": [],
                "normalized_texts": [],
                "digit_stream": "",
                "digit_candidate": None,
                "text_count": 0,
                "uid": None,
            }
            return None
        uid_crop = crop_region_by_box(frame, crop_box=self._uid_crop_box) if frame is not None else None
        uid_signature = self._image_signature(uid_crop)
        now = time.time()
        if (
            uid_signature is not None
            and uid_signature == self._last_uid_signature
            and (now - self._last_uid_ocr_at) < self._uid_ocr_interval
        ):
            self._uid_debug_latest = dict(self._last_uid_debug_cache)
            return self._last_uid_value
        uid, uid_debug = detect_uid_value_with_debug(frame=frame, ocr_engine=ocr, crop_box=self._uid_crop_box)
        self._uid_debug_latest = uid_debug or {}
        self._last_uid_signature = uid_signature
        self._last_uid_ocr_at = now
        self._last_uid_value = uid
        self._last_uid_debug_cache = dict(self._uid_debug_latest)
        
        # 仅在调试模式开启时保存截图到磁盘，避免 I/O 阻塞
        if os.getenv("MC_DEBUG_DUMP", "0") == "1":
            uid_texts = uid_debug.get("raw_texts", []) if uid_debug else []
            self.debug_dumper.dump_uid(frame=frame, crop=uid_crop, uid=uid, texts=uid_texts)
            
        return uid

    def _print_uid_debug(self, stage: str, extra: str = ""):
        now = time.time()
        force = stage in {"locked", "recheck"}
        if not force and now - self._last_uid_debug_print_at < self._uid_debug_print_interval:
            return
        self._last_uid_debug_print_at = now

        dbg = self._uid_debug_latest or {}
        raw_texts = dbg.get("raw_texts") or []
        normalized_texts = dbg.get("normalized_texts") or []
        digit_stream = dbg.get("digit_stream") or ""
        candidate = dbg.get("digit_candidate") or "-"
        crop_shape = dbg.get("crop_shape")
        print(
            "[UID-DEBUG]"
            f" stage={stage}"
            f" uid={dbg.get('uid') or '-'}"
            f" candidate={candidate}"
            f" digits={digit_stream or '-'}"
            f" crop={crop_shape}"
            f" texts={raw_texts[:3]}"
            f" normalized={normalized_texts[:2]}"
            f" {extra}".rstrip()
        )

    def _uid_wait_seconds(self) -> int:
        if self._uid_retry_idx < len(self._uid_retry_steps):
            return self._uid_retry_steps[self._uid_retry_idx]
        return self._uid_retry_steps[-1]

    def _create_or_get_account(
        self,
        uid: str,
        client_started_at: Optional[datetime.datetime],
        client_pid: Optional[int],
    ) -> Optional[int]:
        """创建或获取账号，返回 account.id（int），避免跨 Session 传递 ORM 对象。"""
        if self._disable_db:
            return -1
        sess = self.Session()
        try:
            with sess.write_enabled(self._db_write_key):
                account = sess.query(Account).filter_by(uid=uid).first()
                if not account:
                    account = Account(
                        uid=uid,
                        name=local_machine_name(),
                    )
                    sess.add(account)
                    sess.flush()
                account.name = local_machine_name()
                ensure_account_hash(sess, account)

                is_restarted = mark_client_started(
                    db_session=sess,
                    account=account,
                    detected_started_at=client_started_at,
                    detected_pid=client_pid,
                    write_login_record=True,
                )
                current_game_day = self._compute_game_day_index(local_now().replace(microsecond=0))
                account.today_enhance = (
                    sess.query(EchoSubstat)
                    .filter_by(account_id=account.id, game_day_index=current_game_day)
                    .count()
                )
                account_id = account.id  # 在 Session 关闭前提取标量值
                sess.commit()
            self._just_restarted_client = is_restarted
            self._login_open_count = 0
            self._restart_open_count = 0
            return account_id
        except Exception as e:
            sess.rollback()
            print(f"[Pipeline] account bind error: {e}")
            return None
        finally:
            sess.close()

    def _bind_uid_if_ready(self, frame) -> Dict[str, Any]:
        # UID 绑定状态机：连续 3 次一致才锁定账号，避免单帧误识别。
        status = {
            "uid_locked": self._uid_locked,
            "uid": self._current_uid,
            "uid_wait_seconds": 0,
            "uid_consistent": self._uid_consistent_count,
            "uid_required": self._uid_lock_confirmations,
            "uid_debug": dict(self._uid_debug_latest),
        }

        if self._uid_locked:
            return status

        now = time.time()
        if now < self._next_uid_retry_at:
            # 若上一轮连文本都没识别到，跳过退避直接快速重试，避免错过瞬时可读帧。
            if int((self._uid_debug_latest or {}).get("text_count", 0)) <= 0:
                self._next_uid_retry_at = 0.0
            else:
                status["uid_wait_seconds"] = max(0, int(self._next_uid_retry_at - now))
                self._print_uid_debug(stage="cooldown", extra=f"wait={status['uid_wait_seconds']}s")
                return status

        uid = self._extract_uid_from_frame(frame)
        status["uid_debug"] = dict(self._uid_debug_latest)
        if not uid:
            wait = self._uid_wait_seconds()
            text_count = int((self._uid_debug_latest or {}).get("text_count", 0))
            # OCR 没拿到任何文本时，使用短时等待（而非零等待）避免 CPU 爆满
            # 即使没看到文本，0.5秒重试一次也足够捕捉到出现的 UI
            if text_count <= 0:
                wait = 0.5
            self._next_uid_retry_at = now + wait
            self._uid_retry_idx = min(self._uid_retry_idx + 1, len(self._uid_retry_steps) - 1)
            self._uid_consistent_count = 0
            self._last_uid = None
            status["uid_wait_seconds"] = wait
            self._print_uid_debug(stage="retry", extra=f"wait={wait}s consistent=0/{self._uid_lock_confirmations}")
            return status

        if uid == self._last_uid:
            self._uid_consistent_count += 1
        else:
            self._last_uid = uid
            self._uid_consistent_count = 1
        status["uid"] = uid

        # 一旦已经拿到候选 UID，就不再沿用失败阶段累计出来的长退避。
        # 否则可能出现“已经识别出正确 UID，但还要等 5~10 秒才做下一次确认”的体感卡顿。
        self._uid_retry_idx = 0

        if self._uid_consistent_count >= self._uid_lock_confirmations:
            process_snapshot = self._get_game_process_snapshot()
            client_started_at = process_snapshot.started_at if process_snapshot else None
            client_pid = process_snapshot.pid if process_snapshot else None
            account_id = self._create_or_get_account(
                uid=uid,
                client_started_at=client_started_at,
                client_pid=client_pid,
            )
            if account_id is not None:
                self._uid_locked = True
                self._current_uid = uid
                self._current_account_id = None if self._disable_db else account_id
                self._current_client_started_at = client_started_at
                self._current_client_pid = client_pid
                self._uid_retry_idx = 0
                self._next_uid_retry_at = 0.0
                self._next_uid_recheck_at = time.time() + self._uid_recheck_interval
                status["uid_locked"] = True
                status["uid"] = uid
                status["account_id"] = self._current_account_id
                status["client_pid"] = self._current_client_pid
                status["client_started_at"] = self._current_client_started_at.isoformat() if self._current_client_started_at else None
                self._print_uid_debug(stage="locked", extra=f"uid={uid}")
        else:
            wait = max(0.1, self._uid_candidate_interval)
            self._next_uid_retry_at = now + wait
            status["uid_wait_seconds"] = wait
            self._print_uid_debug(
                stage="candidate",
                extra=f"uid={uid} consistent={self._uid_consistent_count}/{self._uid_lock_confirmations} wait={wait}s",
            )

        status["uid_consistent"] = self._uid_consistent_count
        return status

    def _maybe_refresh_locked_uid(self, frame):
        if self._disable_uid_recheck and self._next_uid_recheck_at > 0:
            return
        if not self._uid_locked:
            return

        now = time.time()
        if now < self._next_uid_recheck_at:
            return
        self._next_uid_recheck_at = now + self._uid_recheck_interval

        uid = self._extract_uid_from_frame(frame)
        if not uid or uid == self._current_uid:
            return

        self._print_uid_debug(stage="recheck", extra=f"switch_from={self._current_uid} switch_to={uid}")

        self._uid_locked = False
        self._current_uid = None
        self._current_account_id = None
        self._current_client_started_at = None
        self._current_client_pid = None
        self._uid_consistent_count = 1
        self._last_uid = uid
        self._next_uid_retry_at = 0.0
        self._uid_retry_idx = 0

    def _compute_game_day_index(self, dt: datetime.datetime) -> int:
        day = dt.date()
        if dt.hour < self.game_day_reset_hour:
            day = day - datetime.timedelta(days=1)
        return int(day.strftime("%Y%m%d"))

    def _extract_observation(
        self,
        raw_texts: List[str],
        parsed: List[Dict[str, Any]],
        conf: float,
        ui_mode_override: Optional[str] = None,
    ) -> EchoObservation:
        # 解析 OCR 文本并构建 EchoObservation 对象，包含当前强化物品的属性、等级、套装信息等，以供后续策略分析和建议生成使用。
        return self.observation_extractor.extract_observation( 
            raw_texts=raw_texts,
            parsed=parsed,
            conf=conf,
            ui_mode_override=ui_mode_override,
        )

    def _resolve_session(self, db_sess, account_id: int, obs: EchoObservation, now_dt: datetime.datetime) -> Optional[EchoInfo]:
        # 声骸信息归并规则：跟随 ActiveEchoContext.local_id。
        # 空白辅音声骸不生成 echo_instance_id；第一个辅音出现时才落 echo_info。
        # 后续新增辅音继续沿用 active context 首次绑定的 echo_instance_id。
        active_context = self._active_echo_context
        if active_context is None:
            return None

        local_id = active_context.local_id
        active = self._active_session_by_account.get(account_id)
        if active and active.get("local_id") == local_id and active.get("echo_instance_id"):
            echo_info = (
                db_sess.query(EchoInfo)
                .filter_by(account_id=account_id, echo_instance_id=active["echo_instance_id"])
                .first()
            )
            if echo_info:
                if obs.level is not None:
                    active["last_level"] = obs.level
                active["cost"] = obs.cost or active.get("cost")
                active["set_name"] = obs.set_name or active.get("set_name")
                active["main_stat"] = obs.main_stat or active.get("main_stat")
                return echo_info

        if not obs.substats:
            return None

        cost = obs.cost
        if cost not in (1, 3, 4):
            return None

        echo_name = str(obs.echo_name or "").strip()
        set_name = str(obs.set_name or "").strip()
        main_stat = str(obs.main_stat or "").strip()
        if not echo_name or echo_name == "未知声骸" or not set_name or not main_stat:
            return None

        echo_instance_id = build_echo_instance_id(
            echo_name=echo_name,
            set_name=set_name,
            main_stat=main_stat,
            substats=obs.substats,
        )
        if not echo_instance_id:
            return None

        account = db_sess.query(Account).filter_by(id=account_id).first()
        uid = str(account.uid if account else (self._current_uid or ""))

        echo_info = (
            db_sess.query(EchoInfo)
            .filter_by(account_id=account_id, echo_instance_id=echo_instance_id)
            .first()
        )
        if echo_info is None:
            echo_info = EchoInfo(
                account_id=account_id,
                uid=uid,
                echo_instance_id=echo_instance_id,
                echo_name=echo_name,
                cost=cost,
                set_name=set_name,
                main_stat=main_stat,
                initial_substat_count=max(1, min(len(obs.substats or []), 5)),
                created_at=now_dt,
            )
            db_sess.add(echo_info)
            db_sess.flush()

        self._active_session_by_account[account_id] = {
            "local_id": local_id,
            "session_id": echo_instance_id,
            "echo_instance_id": echo_instance_id,
            "cost": cost,
            "set_name": set_name,
            "main_stat": main_stat,
            "last_level": obs.level,
        }
        return echo_info

    def _persist_action_events(self, db_sess, account_id: int, echo_session: EchoInfo, obs: EchoObservation, now_dt: datetime.datetime):
        # 按“当前已开孔数 -> 现在已识别孔数”的差集推导本次新增事件。
        existing_events = (
            db_sess.query(EchoSubstat)
            .filter_by(account_id=account_id, session_id=echo_session.echo_instance_id)
            .order_by(EchoSubstat.slot_index.asc())
            .all()
        )
        existing_count = len(existing_events)
        current_count = min(len(obs.substats), 5)
        if current_count <= existing_count:
            return

        new_count = current_count - existing_count
        historical_unknown = existing_count == 0 and (obs.level or 0) > 0
        action_id = make_uuid()
        span_slots = list(range(existing_count + 1, current_count + 1))
        action_type = "history" if historical_unknown else ("multi" if new_count > 1 else "single")
        action_start_level = self._slot_threshold(existing_count + 1)
        action_end_level = self._slot_threshold(current_count)
        action_span_holes = ",".join(str(s) for s in span_slots)

        game_day_index = self._compute_game_day_index(now_dt)
        account = db_sess.query(Account).filter_by(id=account_id).first()
        account_total = int(account.total_enhance or 0) if account else db_sess.query(EchoSubstat).filter(
            EchoSubstat.account_id == account_id,
            EchoSubstat.action_type != "history"
        ).count()
        client_total = int(account.client_enhance or 0) if account else 0
        day_total = db_sess.query(EchoSubstat).filter_by(account_id=account_id, game_day_index=game_day_index).count()

        for offset, slot_index in enumerate(span_slots, start=1):
            sub = obs.substats[slot_index - 1]
            value_tier = None
            if isinstance(sub.get("value"), (int, float)):
                value_tier = 4 if float(sub["value"]) >= 10 else 1

            self._login_open_count += 1
            if self._just_restarted_client:
                self._restart_open_count += 1

            event = EchoSubstat(
                event_id=make_uuid(),
                session_id=echo_session.echo_instance_id,
                action_id=action_id,
                account_id=account_id,
                action_type=action_type,
                action_open_count=new_count,
                action_start_level=action_start_level,
                action_end_level=action_end_level,
                action_span_holes=action_span_holes,
                slot_index=slot_index,
                level_before=self._slot_threshold(slot_index),
                substat_name=str(sub.get("name", "未知副词条")),
                substat_value=float(sub.get("value", 0.0)),
                value_tier=value_tier,
                is_historical_unknown=historical_unknown,
                game_day_index=game_day_index if action_type != "history" else None,
                is_first_enhance_of_day=(day_total + offset == 1) if action_type != "history" else None,
                is_just_logged_in=(self._login_open_count == 1) if action_type != "history" else None,
                is_just_client_restarted=((self._just_restarted_client and self._restart_open_count == 1) if action_type != "history" else None),
                restart_open_index=(self._restart_open_count if (self._just_restarted_client and action_type != "history") else None),
                day_enhance_count=(day_total + offset) if action_type != "history" else None,
                source_region=obs.source_region,
                ocr_confidence=obs.ocr_confidence,
                created_at=now_dt,
            )
            db_sess.add(event)

        if account:
            if action_type != "history":
                account.total_enhance = account_total + new_count
                account.today_enhance = day_total + new_count
                account.client_enhance = client_total + new_count
                db_sess.add(account)

        if self._just_restarted_client and self._restart_open_count > 0:
            self._just_restarted_client = False

    def tick(self) -> Optional[Dict[str, Any]]:
        """执行一次 采集 → 检测 → OCR → 解析 → 概率 并返回结果。"""
        timings: Dict[str, float] = {}
        # 阶段 1：进程守卫。游戏没开时直接给 UI 返回等待状态。
        if not self._is_game_running():
            self._clear_uid_binding_after_process_change()
            return {
                "status": "waiting_game_process",
                "detections": [],
                "tick": self._tick_count,
            }

        # 优化点：直接引用 CaptureWorker 的内存缓冲，避免全帧拷贝 (Zero-Copy)
        # CaptureWorker 保证返回的是线程安全的独立帧对象 (dxcam/BitBlt 产生的副本)
        frame = self.capture.frame
        if frame is None:
            return None
        
        # 注意：后续流程可能较慢，先返回 UID 相关状态给 UI，避免界面长时间无响应。
        t_stage = time.perf_counter()
        self._reload_strategy_profile_if_needed()
        self._reload_echo_dictionary_if_needed()
        timings["reload"] = (time.perf_counter() - t_stage) * 1000

        t_stage = time.perf_counter()
        if not self._disable_uid:
            self._maybe_refresh_locked_uid(frame)
        timings["uid_recheck"] = (time.perf_counter() - t_stage) * 1000

        # 阶段 2：UID 绑定守卫。未锁定账号时不进入后续统计与入库。
        t_stage = time.perf_counter()
        if self._disable_uid:
            self._uid_locked = True
            self._current_uid = self._current_uid or "UID_DISABLED"
            uid_status = {
                "uid_locked": True,
                "uid": self._current_uid,
                "uid_wait_seconds": 0,
                "uid_consistent": self._uid_lock_confirmations,
                "uid_required": self._uid_lock_confirmations,
                "uid_debug": {},
            }
        else:
            uid_status = self._bind_uid_if_ready(frame)
        timings["uid_bind"] = (time.perf_counter() - t_stage) * 1000
        if not uid_status["uid_locked"]:
            self._tick_count += 1
            out = {
                "status": "waiting_uid",
                "uid": uid_status.get("uid") or self._current_uid,
                "account_id": self._current_account_id,
                "uid_status": uid_status,
                "uid_crop_box": list(self._uid_crop_box),
                "strategy_config": self._strategy_config_status(),
                "frame_shape": list(frame.shape),
                "detections": [],
                "tick": self._tick_count,
                "capture_fps": round(self.capture.actual_fps, 1),
            }
            self._log_stage_timing(timings)
            if self.on_result:
                self.on_result(out)
            return out

        t0 = time.perf_counter()

        if self._capture_only:
            self._tick_count += 1
            out = self._build_base_out("capture_only", frame, self.capture.actual_fps)
            timings["total"] = (time.perf_counter() - t0) * 1000
            self._log_stage_timing(timings)
            if self.on_result:
                self.on_result(out)
            return out

        # ── 场景判定 + 检测 ──
        # 阶段 3：使用 YOLO 模型检测场景和面板位置。
        t_stage = time.perf_counter()
        scene_panel_bbox = None
        detector = self._ensure_detector()
        if detector is not None and detector.ready:
            scene_info = self._detect_scene_by_yolo(frame)
            scene_name = scene_info.get("scene", "unknown")
            scene_panel_bbox = scene_info.get("bbox")
        else:
            scene_name = "unknown"

        # // 生成场景特征签名，用于结果复用判断。仅当场景已知时才生成签名，未知场景不启用复用。
        scene_signature = self._scene_panel_signature(frame, scene_name, panel_bbox_override=scene_panel_bbox) if scene_name != "unknown" else None
        timings["scene_match"] = (time.perf_counter() - t_stage) * 1000

        if (
            not self._disable_result_reuse
            and
            scene_signature is not None
            and self._last_scene_signature == scene_signature
            and self._last_detection_out is not None
        ):
            self._tick_count += 1
            out = self._reuse_last_detection_out(frame, self.capture.actual_fps)
            self._log_stage_timing(timings)
            if self.on_result:
                self.on_result(out)
            return out

        detections = []
        t_stage = time.perf_counter()
        if scene_name != "unknown" and not self._disable_panel_ocr:
            scene_ocr = self._ocr_scene_regions(frame, scene_name, panel_bbox_override=scene_panel_bbox)
            if scene_ocr:
                detections = [{
                    "bbox": scene_ocr["bbox"],
                    "conf": 1.0,
                    "cls": -2,
                    "scene": scene_name,
                    "ui_mode": scene_ocr.get("ui_mode"),
                    "raw_texts": scene_ocr.get("raw_texts", []),
                }]
        timings["scene_ocr"] = (time.perf_counter() - t_stage) * 1000
        
        # 逻辑变更：不再使用 YOLO 或通用固定区域回退。
        # 仅当像素点精确匹配到已知 UI 场景时才进行后续识别。

        # 阶段 4：对每个候选区域做 OCR/解析，并将结果写入数据库。
        results: List[Dict[str, Any]] = []
        now_dt = local_now().replace(microsecond=0)
        db_sess = None if self._disable_db else self.Session()
        if db_sess is not None:
            db_sess.authorize_writes(self._db_write_key)
        for det in detections:
            crop = crop_bbox(frame, det["bbox"])
            if crop.size == 0:
                continue

            raw_texts = det.get("raw_texts") or []
            if not raw_texts:
                # ── 常规 OCR 路径：用原色彩裁剪图做 OCR，不做二值化 ──
                ocr = self._ensure_ocr()
                if ocr is None:
                    continue
                ocr_input = upscale_if_small(crop, min_height=64)
                ocr_res = ocr.recognize_with_boxes(ocr_input)

                # 如果原图 OCR 无结果，回退尝试二值化增强图
                if not ocr_res:
                    processed = enhance_for_ocr(crop)
                    ocr_res = ocr.recognize_with_boxes(processed)
                    ocr_input = processed  # 更新图以供调试 dumper 使用
                
                raw_texts = group_ocr_texts_by_y(ocr_res, y_threshold=15)
            else:
                ocr_input = crop

            # ── 调试日志 ──
            now_t = time.time()
            if now_t - self._last_echo_log_at > 2.0:
                print(
                    f"[Pipeline-Echo] tick={self._tick_count}"
                    f"  crop={list(crop.shape)}"
                    f"  bbox={det['bbox']}"
                    f"  scene={det.get('scene', scene_name)}"
                    f"  ocr_count={len(raw_texts)}"
                    f"  texts={raw_texts[:5]}"
                )
                self._last_echo_log_at = now_t

            if os.getenv("MC_DEBUG_DUMP", "0") == "1":
                self.debug_dumper.dump_panel(
                    frame=frame,
                    crop=crop,
                    processed=ocr_input,
                    det_index=len(results),
                    raw_texts=raw_texts,
                )
            
            parsed = parse_texts(raw_texts)

            # 更新概率模型（以标准化词缀名为 key）
            for p in parsed:
                self.freq_model.update(p["name"])
                self.bayes.update(p["name"])

            result = {
                "bbox": det["bbox"],
                "conf": det["conf"],
                "scene": det.get("scene", scene_name),
                "ui_mode": det.get("ui_mode"),
                "raw_texts": raw_texts,
                "parsed": parsed,
                "freq_prob": self.freq_model.predict(),
                "bayes_prob": self.bayes.posterior(),
                "timestamp": time.time(),
            }
            obs: Optional[EchoObservation] = None
            echo_instance_id_for_view: Optional[str] = None

            try:
                if self._current_account_id and db_sess is not None:
                    obs = self._extract_observation(
                        raw_texts,
                        parsed,
                        conf=det["conf"],
                        ui_mode_override=det.get("ui_mode"),
                    )
                    obs = self._resolve_active_echo_observation(
                        obs,
                        scene=det.get("scene") or det.get("ui_mode"),
                        now=now_t,
                    )
                    result["echo_observation"] = {
                        "level": obs.level,
                        "cost": obs.cost,
                        "ui_mode": obs.ui_mode,
                        "slot_states": obs.slot_states,
                        "echo_name": obs.echo_name,
                        "set_name": obs.set_name,
                        "main_stat": obs.main_stat,
                        "equipment": obs.equipment,
                        "substats": obs.substats,
                    }
                    if not self._disable_strategy:
                        result["strategy_advice"] = self._estimate_action_advice(
                            db_sess=db_sess,
                            account_id=self._current_account_id,
                            obs=obs,
                        )
                    else:
                        _, role_source, selected_role, available_roles = self._resolve_strategy_role(
                            obs.set_name,
                            equipment=obs.equipment,
                        )
                        perfect_recommend = self._resolve_perfect_recommendation(obs.set_name, selected_role)
                        cost_analysis = self.action_advisor.analyze_cost(
                            obs.substats,
                            perfect_consonant=perfect_recommend["consonant"],
                            fallback=self.strategy_profile.fallback_weight,
                        )
                        result["strategy_advice"] = {
                            "recommended_action": "disabled",
                            "reason": "strategy disabled by MC_DISABLE_STRATEGY",
                            "single_score": 0.0,
                            "multi_score": 0.0,
                            "single_samples": 0,
                            "multi_samples": 0,
                            "tunable_slots_count": 0,
                            "priority_set": obs.set_name,
                            "priority_source": "disabled",
                            "priority_weight_hits": [],
                            "strategy_role_selected": selected_role,
                            "strategy_role_available": available_roles,
                            "strategy_role_source": role_source,
                            "perfect_cost_main_stats": perfect_recommend["cost_main_stats"],
                            "perfect_consonant": perfect_recommend["consonant"],
                            "perfect_source": perfect_recommend["source"],
                            "substat_probabilities": {},
                            "substat_probability_samples": 0,
                            "substat_posterior": {},
                            "cost_analysis": cost_analysis,
                        }
                    echo_session = self._resolve_session(db_sess, self._current_account_id, obs, now_dt)
                    if echo_session is not None:
                        echo_instance_id_for_view = echo_session.echo_instance_id
                        self._persist_action_events(db_sess, self._current_account_id, echo_session, obs, now_dt)
                        current_game_day = self._compute_game_day_index(now_dt)
                        out_account_stats = {
                            "total_enhance": account.total_enhance,
                            "today_enhance": db_sess.query(EchoSubstat).filter_by(
                                account_id=self._current_account_id, game_day_index=current_game_day
                            ).count(),
                            "client_enhance": account.client_enhance,
                        }
            except Exception as e:
                print(f"[Pipeline] event persist error: {e}")

            if obs is None:
                # 即便未绑定账号，也提供槽位状态用于UI展示和联调。
                obs = self._extract_observation(
                    raw_texts,
                    parsed,
                    conf=det["conf"],
                    ui_mode_override=det.get("ui_mode"),
                )
                obs = self._resolve_active_echo_observation(
                    obs,
                    scene=det.get("scene") or det.get("ui_mode"),
                    now=now_t,
                )
                result["echo_observation"] = {
                    "level": obs.level,
                    "cost": obs.cost,
                    "ui_mode": obs.ui_mode,
                    "slot_states": obs.slot_states,
                    "echo_name": obs.echo_name,
                    "set_name": obs.set_name,
                    "main_stat": obs.main_stat,
                    "equipment": obs.equipment,
                    "substats": obs.substats,
                }
                _, role_source, selected_role, available_roles = self._resolve_strategy_role(
                    obs.set_name,
                    equipment=obs.equipment,
                )
                perfect_recommend = self._resolve_perfect_recommendation(obs.set_name, selected_role)
                cost_analysis = self.action_advisor.analyze_cost(
                    obs.substats,
                    perfect_consonant=perfect_recommend["consonant"],
                    fallback=self.strategy_profile.fallback_weight,
                )
                result["strategy_advice"] = {
                    "recommended_action": "single",
                    "reason": "未绑定账号，使用默认单开建议",
                    "single_score": 0.0,
                    "multi_score": 0.0,
                    "single_samples": 0,
                    "multi_samples": 0,
                    "tunable_slots_count": sum(
                        1 for s in obs.slot_states if s.get("status") in ("current_tunable", "pending_tunable")
                    ),
                    "priority_set": obs.set_name,
                    "priority_source": "default",
                    "priority_weight_hits": [
                        {
                            "substat_name": str(s.get("name", "")),
                            "weight": 0.5,
                        }
                        for s in obs.substats
                        if str(s.get("name", ""))
                    ],
                    "strategy_role_selected": selected_role,
                    "strategy_role_available": available_roles,
                    "strategy_role_source": role_source,
                    "perfect_cost_main_stats": perfect_recommend["cost_main_stats"],
                    "perfect_consonant": perfect_recommend["consonant"],
                    "perfect_source": perfect_recommend["source"],
                    "substat_probabilities": {},
                    "substat_probability_samples": 0,
                    "substat_posterior": {},
                    "cost_analysis": cost_analysis,
                }
            if obs is not None:
                result["echo"] = self._build_echo_view(
                    obs,
                    result.get("strategy_advice"),
                    echo_instance_id=echo_instance_id_for_view,
                )

            # ── 声骸识别结果日志 ──
            if obs and obs.echo_name != "未知声骸":
                sub_desc = ", ".join(
                    f"{s.get('name','?')}:{s.get('value',0)}" for s in (obs.substats or [])
                )
                print(
                    f"[Pipeline-Obs] 声骸={obs.echo_name}  套装={obs.set_name}"
                    f"  主词条={obs.main_stat}  等级=+{obs.level}  COST={obs.cost}"
                    f"  副词条=[{sub_desc}]"
                )

            results.append(result)

        if db_sess is not None:
            try:
                db_sess.commit()
            except Exception as e:
                db_sess.rollback()
                print(f"[Pipeline] db commit error: {e}")
            finally:
                db_sess.disable_writes()
                db_sess.close()

        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._tick_count += 1

        public_results = [self._compact_detection_for_ui(r) for r in results]
        
        # 补充全局账号状态，以便 UI 直接展示强化次数等信息
        account_stats = None
        if self._current_account_id:
            try:
                stat_sess = self.Session()
                acc_rec = stat_sess.query(Account).filter_by(id=self._current_account_id).first()
                if acc_rec:
                    # TODO: 从远程服务器获取大数据强化统计，暂留空
                    global_total_enhance = 0
                    global_today_enhance = 0
                    
                    contribution_rate = 0.0
                    if global_total_enhance > 0:
                        contribution_rate = acc_rec.total_enhance / global_total_enhance
                        
                    account_stats = {
                        "total_enhance": acc_rec.total_enhance,
                        "today_enhance": acc_rec.today_enhance,
                        "client_enhance": acc_rec.client_enhance,
                        "global_total_enhance": global_total_enhance,
                        "global_today_enhance": global_today_enhance,
                        "contribution_rate": contribution_rate,
                    }
            except Exception:
                pass
            finally:
                if 'stat_sess' in locals():
                    stat_sess.close()

        out = self._build_base_out("running", frame, self.capture.actual_fps, public_results)
        if account_stats:
            out["account_stats"] = account_stats

        selected_echo = self._select_echo_for_output(public_results, self._last_echo_view)
        if selected_echo:
            out["echo"] = selected_echo
            self._last_echo_view = selected_echo
        out["scene_match"] = scene_info
        out["latency_ms"] = round(elapsed_ms, 1)
        if public_results:
            self._last_scene_signature = scene_signature
            self._last_detection_out = dict(out)
        elif scene_name == "unknown":
            self._last_scene_signature = None
            self._last_detection_out = None
        timings["total"] = elapsed_ms
        self._log_stage_timing(timings)
        if self.on_result:
            self.on_result(out)
        return out

# ── CLI 自测 ──────────────────────────────────────────────────
def main():
    runner = PipelineRunner(use_gpu=True)
    runner.start_capture()
    print("[Pipeline] 等待游戏窗口...")
    try:
        for i in range(120):
            out = runner.tick()
            if out and out["detections"]:
                echo = out.get("echo") or {}
                print(
                    f"  echo={echo.get('echo_name')} {echo.get('level_text')}"
                    f" set={echo.get('set_name')} main={echo.get('main_stat')}"
                )
                print(f"  latency={out['latency_ms']}ms  capture_fps={out['capture_fps']}")
            else:
                print(f"[{i}] no result")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        print("[Pipeline] stopped")


if __name__ == "__main__":
    main()
