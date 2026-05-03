"""
ocr.py
基于 RapidOCR (ONNX DirectML) 封装的 OCR 模块，全面舍弃不稳定的 PaddleOCR/torch-combo，
并强制要求使用 GPU 进行推理，剥离一切关于 CPU 慢速执行的可能。

特点：
1. 彻底禁用 CPU OCR
2. DML (DirectML) Windows 下硬件加速 GPU
"""
import os
from typing import List, Tuple, Dict, Any, Optional

try:
    from rapidocr_onnxruntime import RapidOCR
    _RAPIDOCR_AVAILABLE = True
except ImportError as e:
    _RAPIDOCR_AVAILABLE = False
    _RAPIDOCR_IMPORT_ERROR = repr(e)

class OCR:
    def __init__(self, lang: str = "ch", use_gpu: bool = True):
        self.lang = lang
        self.requested_gpu = use_gpu
        self.use_gpu = bool(use_gpu)
        self.model = None
        self.backend = "unavailable"
        self.disabled_reason = None
        
        if not self.use_gpu:
            self.disabled_reason = "gpu_required"
            self.backend = "disabled-gpu-required"
            print("[OCR] 极速识别要求强制使用 GPU，当前已被重置或禁用 (use_gpu=False)")
            return

        if not _RAPIDOCR_AVAILABLE:
            self.disabled_reason = "rapidocr_not_installed"
            self.backend = "disabled-no-ocr"
            print(f"[OCR] RapidOCR 未安装: {_RAPIDOCR_IMPORT_ERROR}")
            return

        try:
            print("[OCR] 正在初始化 RapidOCR (ONNX DirectML GPU)...")
            # 强制开启 DML，如果不支持会抛出回退/不使用
            self.model = RapidOCR(
                det_use_dml=True,
                cls_use_dml=True,
                rec_use_dml=True
            )
            self.backend = "rapidocr-gpu"
            print(f"[OCR] backend={self.backend} mode=GPU-only")
        except Exception as e:
            self.disabled_reason = "rapidocr_init_failed"
            self.backend = "disabled-rapidocr-failed"
            print(f"[OCR] 初始化 RapidOCR 失败: {e}")

    def _run_rapid(self, image) -> List[Tuple[List, str, float]]:
        if not self.model:
            return []
        try:
            results, _ = self.model(image)
        except Exception as e:
            print(f"[OCR] rapid 推理异常: {e}")
            return []
        
        out: List[Tuple[List, str, float]] = []
        for line in results or []:
            if isinstance(line, (list, tuple)) and len(line) >= 3:
                box = line[0]
                text = str(line[1])
                try:
                    score = float(line[2])
                except Exception:
                    score = 0.0
                out.append((box, text, score))
        return out

    def recognize(self, image) -> List[str]:
        """识别图片中的文字，返回文本列表。"""
        if self.backend.startswith("disabled"):
            return []
        try:
            parsed = self._run_rapid(image)
        except Exception as e:
            print(f"[OCR] 识别失败: {e}")
            return []
        return [text for _, text, _ in parsed]

    def recognize_with_boxes(self, image) -> List[Tuple[List, str, float]]:
        """返回 [(bbox_points, text, confidence), ...]。"""
        if self.backend.startswith("disabled"):
            return []
        try:
            return self._run_rapid(image)
        except Exception as e:
            print(f"[OCR] 识别失败: {e}")
            return []

if __name__ == "__main__":
    import numpy as np
    import cv2
    img = np.ones((50, 400, 3), dtype=np.uint8) * 255
    cv2.putText(img, "TEST", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,0), 2)
    ocr = OCR(use_gpu=True)
    res = ocr.recognize(img)
    print("Test output:", res)
