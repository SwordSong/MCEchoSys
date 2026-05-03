"""
detector.py — 视觉检测层模块。

发布版优先使用 YOLO ONNX + ONNX Runtime/DirectML，避免把 CUDA Torch
运行库打进包里。开发环境仍可回退加载 ultralytics 的 .pt 模型。
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np


class Detector:
    def __init__(
        self,
        model_path: Optional[str] = None,
        device: str = "cuda",
        half: bool = False,
    ):
        self.model_path = model_path
        self.device = device
        self.half = half
        self.model = None
        self._backend = ""
        self._ort_session = None
        self._ort_input_name = ""
        self._ort_input_size: Tuple[int, int] = (640, 640)

        if not model_path or not os.path.exists(model_path):
            if model_path:
                print(f"[Detector] model not found: {model_path}")
            return

        suffix = Path(model_path).suffix.lower()
        if suffix == ".onnx":
            self._load_onnx(model_path)
        else:
            self._load_ultralytics(model_path)

    def _load_onnx(self, model_path: str):
        try:
            import onnxruntime as ort
        except Exception as e:
            print(f"[Detector] onnxruntime unavailable: {e}")
            return

        available = set(ort.get_available_providers())
        providers = []
        if "DmlExecutionProvider" in available:
            providers.append("DmlExecutionProvider")
        providers.append("CPUExecutionProvider")

        try:
            self._ort_session = ort.InferenceSession(model_path, providers=providers)
            model_input = self._ort_session.get_inputs()[0]
            self._ort_input_name = model_input.name
            shape = list(model_input.shape)
            height = shape[2] if len(shape) > 2 and isinstance(shape[2], int) else 640
            width = shape[3] if len(shape) > 3 and isinstance(shape[3], int) else 640
            self._ort_input_size = (int(width), int(height))
            self._backend = "onnx"
            self.model = self._ort_session
            print(
                f"[Detector] loaded ONNX {model_path}"
                f" providers={self._ort_session.get_providers()}"
                f" input={self._ort_input_size[0]}x{self._ort_input_size[1]}"
            )
        except Exception as e:
            print(f"[Detector] ONNX load failed: {e}")
            self._ort_session = None
            self.model = None
            self._backend = ""

    def _load_ultralytics(self, model_path: str):
        try:
            ultralytics = importlib.import_module("ultralytics")
            yolo_cls = getattr(ultralytics, "YOLO")
        except Exception as e:
            print(f"[Detector] ultralytics unavailable: {e}")
            return

        try:
            self.model = yolo_cls(model_path)
            self._backend = "ultralytics"
            print(f"[Detector] loaded PT {model_path} on {self.device}")
        except Exception as e:
            print(f"[Detector] PT load failed: {e}")
            self.model = None
            self._backend = ""

    @property
    def ready(self) -> bool:
        return self.model is not None

    @staticmethod
    def _letterbox(image: np.ndarray, target_size: Tuple[int, int]):
        target_w, target_h = target_size
        src_h, src_w = image.shape[:2]
        scale = min(target_w / src_w, target_h / src_h)
        new_w = int(round(src_w * scale))
        new_h = int(round(src_h * scale))
        pad_x = (target_w - new_w) / 2
        pad_y = (target_h - new_h) / 2

        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((target_h, target_w, 3), 114, dtype=np.uint8)
        left = int(round(pad_x - 0.1))
        top = int(round(pad_y - 0.1))
        canvas[top : top + new_h, left : left + new_w] = resized
        return canvas, scale, left, top

    def _predict_onnx(self, image: np.ndarray, conf: float) -> List[Dict[str, Any]]:
        if self._ort_session is None:
            return []

        input_img, scale, pad_x, pad_y = self._letterbox(image, self._ort_input_size)
        rgb = cv2.cvtColor(input_img, cv2.COLOR_BGR2RGB)
        blob = rgb.transpose(2, 0, 1)[None].astype(np.float32) / 255.0

        try:
            outputs = self._ort_session.run(None, {self._ort_input_name: blob})
        except Exception as e:
            print(f"[Detector] ONNX predict error: {e}")
            return []

        if not outputs:
            return []

        pred = np.asarray(outputs[0])
        pred = np.squeeze(pred)
        if pred.ndim != 2:
            return []

        # YOLOv8 ONNX commonly outputs (attrs, anchors); convert to (anchors, attrs).
        if pred.shape[0] < pred.shape[1] and pred.shape[0] <= 256:
            pred = pred.T

        boxes_xywh: List[List[int]] = []
        boxes_xyxy: List[List[float]] = []
        scores: List[float] = []
        classes: List[int] = []
        src_h, src_w = image.shape[:2]

        for row in pred:
            if row.shape[0] < 6:
                continue

            class_scores = row[4:]
            cls = int(np.argmax(class_scores))
            score = float(class_scores[cls])
            if score < conf:
                continue

            cx, cy, w, h = [float(v) for v in row[:4]]
            x1 = (cx - w / 2 - pad_x) / scale
            y1 = (cy - h / 2 - pad_y) / scale
            x2 = (cx + w / 2 - pad_x) / scale
            y2 = (cy + h / 2 - pad_y) / scale

            x1 = max(0.0, min(float(src_w - 1), x1))
            y1 = max(0.0, min(float(src_h - 1), y1))
            x2 = max(0.0, min(float(src_w - 1), x2))
            y2 = max(0.0, min(float(src_h - 1), y2))
            if x2 <= x1 or y2 <= y1:
                continue

            boxes_xyxy.append([x1, y1, x2, y2])
            boxes_xywh.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
            scores.append(score)
            classes.append(cls)

        if not boxes_xywh:
            return []

        indices = cv2.dnn.NMSBoxes(boxes_xywh, scores, conf, 0.45)
        flat_indices = np.array(indices).reshape(-1).tolist() if len(indices) else []

        return [
            {"bbox": boxes_xyxy[i], "conf": float(scores[i]), "cls": int(classes[i])}
            for i in flat_indices
        ]

    def _predict_ultralytics(self, image, conf: float) -> List[Dict[str, Any]]:
        try:
            results = self.model(
                image,
                conf=conf,
                device=self.device,
                half=self.half,
                verbose=False,
            )
        except Exception as e:
            print(f"[Detector] PT predict error: {e}")
            return []

        out: List[Dict[str, Any]] = []
        for r in results:
            boxes = r.boxes
            for b in boxes:
                xyxy = b.xyxy[0].cpu().numpy().tolist()
                confv = float(b.conf[0].cpu().numpy())
                cls = int(b.cls[0].cpu().numpy())
                out.append({"bbox": xyxy, "conf": confv, "cls": cls})
        return out

    def predict(self, image, conf: float = 0.3) -> List[Dict[str, Any]]:
        """
        传入单帧画面，返回命中的检测框。

        :return: [{"bbox": [x1, y1, x2, y2], "conf": 0.99, "cls": 类别ID}, ...]
        """
        if not self.model:
            return []
        if self._backend == "onnx":
            return self._predict_onnx(image, conf)
        return self._predict_ultralytics(image, conf)


if __name__ == "__main__":
    det = Detector(model_path=None)
    print(f"Detector ready={det.ready}")
