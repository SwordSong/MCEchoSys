"""
detect_feature_code.py

UID 特征码识别模块（可复用）。

提供：
- detect_uid_value(frame, ocr_engine, crop_box) -> Optional[str]
- detect_uid_bool(frame, ocr_engine, crop_box) -> bool

CLI 调试：
- uv run python -m src.detect_feature_code
"""
from __future__ import annotations

import os
import re
import time
from typing import List, Optional, Tuple, Dict, Any

import numpy as np

try:
    import cv2
except Exception:
    cv2 = None

from src.capture import CaptureWorker
from src.recognition_debug import RecognitionDebugDumper
from src.resources import runtime_logs_dir

# (top, left, bottom, right)
# 右下角 UID 区域，适配“特征码: 123456789”常见位置
DEFAULT_CROP_BOX: Tuple[float, float, float, float] = (0.98, 0.88, 1.0, 0.98)
_UID_RE = re.compile(r"\d{9}")
_DIGIT_LIKE_RE = re.compile(r"[0-9OoIl|SBZzGgQq]{9,}")
 
_DIGIT_FIX_MAP = {
    "O": "0",
    "o": "0",
    "I": "1",
    "l": "1",
    "|": "1",
    "S": "5",
    "s": "5",
    "B": "8",
    "Z": "2",
    "z": "2",
    "G": "6",
    "g": "9",
    "Q": "0",
    "q": "9",
}


def get_uid_crop_box() -> Tuple[float, float, float, float]:
    raw = os.getenv("MC_UID_CROP_BOX", "").strip()
    if not raw:
        return DEFAULT_CROP_BOX
    try:
        parts = [float(item.strip()) for item in raw.split(",")]
        if len(parts) != 4:
            return DEFAULT_CROP_BOX
        top, left, bottom, right = parts
        if top >= bottom or left >= right:
            return DEFAULT_CROP_BOX
        return (
            max(0.0, min(1.0, top)),
            max(0.0, min(1.0, left)),
            max(0.0, min(1.0, bottom)),
            max(0.0, min(1.0, right)),
        )
    except Exception:
        return DEFAULT_CROP_BOX


# // 下面是核心功能实现，提供 UID 特征码识别能力。
# // detect_uid_value: 返回识别到的 UID 特征码字符串，或 None 如果未识别。
# // detect_uid_bool: 返回是否成功识别到 UID 特征码（True/False）。
def crop_region_by_box(frame: np.ndarray, crop_box: Tuple[float, float, float, float] = DEFAULT_CROP_BOX) -> np.ndarray:
    h, w = frame.shape[:2]
    top_frac, left_frac, bottom_frac, right_frac = crop_box

    top_frac = max(0.0, min(1.0, top_frac))
    left_frac = max(0.0, min(1.0, left_frac))
    bottom_frac = max(0.0, min(1.0, bottom_frac))
    right_frac = max(0.0, min(1.0, right_frac))

    y0 = int(frame.shape[0] * top_frac)
    y1 = int(frame.shape[0] * bottom_frac)
    x0 = int(frame.shape[1] * left_frac)
    x1 = int(frame.shape[1] * right_frac)

    # Convert to standard Python int to avoid numpy warnings
    y0 = int(max(0, min(y0, frame.shape[0] - 1)))
    y1 = int(max(y0 + 1, min(y1, frame.shape[0])))
    x0 = int(max(0, min(x0, frame.shape[1] - 1)))
    x1 = int(max(x0 + 1, min(x1, frame.shape[1])))

    # Return a slice (no copy)
    # The caller is responsible for copying if they need to modify it or keep it long-term
    # But wait, OCR engines often require contiguous memory for best performance
    # However, copying every crop is expensive if not needed.
    # We will let OCR handle it or do it lazily.
    return frame[y0:y1, x0:x1]


def extract_uid(text: str) -> Optional[str]:
    m = _UID_RE.search(text or "")
    return m.group(0) if m else None


# 以下是一些辅助函数，用于处理 OCR 输出和文本清洗，提升识别准确率。
def _ocr_texts(ocr_engine, image) -> List[str]:
    if ocr_engine is None:
        return []

    # 优先兼容项目 OCR 封装：OCR.recognize(image) -> List[str]
    if hasattr(ocr_engine, "recognize"):
        try:
            out = ocr_engine.recognize(image)
            return [str(x) for x in (out or [])]
        except Exception:
            return []

    # 兼容直接可调用 OCR 引擎：ocr_engine(image) -> (results, _)
    try:
        results, _ = ocr_engine(image)
    except Exception:
        return []

    texts: List[str] = []
    for line in results or []:
        if line and len(line) >= 2:
            texts.append(str(line[1]))
    print(f"[OCR] extracted texts: {texts}")
    return texts


def _ocr_texts_with_boxes(ocr_engine, image) -> List[str]:
    if ocr_engine is None or not hasattr(ocr_engine, "recognize_with_boxes"):
        return []
    try:
        out = ocr_engine.recognize_with_boxes(image)
    except Exception:
        return []
    texts: List[str] = []
    for item in out or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            texts.append(str(item[1]))
    return texts


def _uid_preprocess_variants(crop: np.ndarray) -> List[np.ndarray]:
    """生成多种 UID 识别预处理图，提升浅色背景/描边文本识别率。"""
    if crop is None or getattr(crop, "size", 0) == 0:
        return []

    # 简化变体生成策略：仅保留最有效的几种，避免无谓的 CPU 开销
    # 策略 1: 原图
    variants = [crop]
    if cv2 is None:
        return variants

    try:
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    except Exception:
        return variants

    # 策略 2: 2倍放大 (处理小字体的关键)
    try:
        up2 = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
        variants.append(cv2.cvtColor(up2, cv2.COLOR_GRAY2BGR))
    except Exception:
        up2 = gray

    # 策略 3: 自适应二值化 (处理复杂背景干扰)
    try:
        # 使用上一步的 up2 或 gray
        blur = cv2.GaussianBlur(up2, (3, 3), 0)
        # 仅保留二值化反色 (因为 UID 通常为浅色字深色底，反色变黑字白底利于 OCR)
        th2 = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 6)
        variants.append(cv2.cvtColor(th2, cv2.COLOR_GRAY2BGR))
    except Exception:
        pass
    
    return variants


def _extract_uid_from_texts(texts: List[str]) -> Optional[str]:
    def normalize_digit_like(s: str) -> str:
        s = s.replace("特征码", "").replace("特徵碼", "").replace("UID", "")
        s = s.replace(":", "").replace("：", "").replace(" ", "")
        return "".join(_DIGIT_FIX_MAP.get(ch, ch) for ch in s)

    for t in texts:
        uid = extract_uid(t)
        if uid:
            return uid

    for t in texts:
        normalized = normalize_digit_like(t)
        uid = extract_uid(normalized)
        if uid:
            return uid

        m = _DIGIT_LIKE_RE.search(normalized)
        if m:
            digit_like = "".join(ch for ch in m.group(0) if ch.isdigit())
            if len(digit_like) >= 9:
                return digit_like[:9]

    joined = "".join(texts)
    uid = extract_uid(joined)
    if uid:
        return uid

    normalized_joined = normalize_digit_like(joined)
    uid = extract_uid(normalized_joined)
    if uid:
        return uid

    # 兜底：去掉非数字后尝试从长串中截取 9 位
    digits = "".join(ch for ch in normalized_joined if ch.isdigit())
    if len(digits) >= 9:
        return digits[:9]
    return None


def _collect_uid_debug(texts: List[str]) -> Dict[str, Any]:
    def normalize_digit_like(s: str) -> str:
        s = s.replace("特征码", "").replace("特徵碼", "").replace("UID", "")
        s = s.replace(":", "").replace("：", "").replace(" ", "")
        return "".join(_DIGIT_FIX_MAP.get(ch, ch) for ch in s)

    normalized_texts = [normalize_digit_like(t) for t in texts if t]
    joined = "".join(normalized_texts)
    digits = "".join(ch for ch in joined if ch.isdigit())
    candidate_uid = digits[:9] if len(digits) >= 9 else None

    return {
        "raw_texts": [str(t) for t in texts[:12]],
        "normalized_texts": normalized_texts[:12],
        "digit_stream": digits[:32],
        "digit_candidate": candidate_uid,
        "text_count": len(texts),
    }


def detect_uid_value_with_debug(
    frame: np.ndarray,
    ocr_engine,
    crop_box: Tuple[float, float, float, float] = DEFAULT_CROP_BOX,
) -> Tuple[Optional[str], Dict[str, Any]]:
    debug = {
        "crop_box": list(crop_box),
        "crop_shape": None,
        "variant_count": 0,
        "raw_texts": [],
        "normalized_texts": [],
        "digit_stream": "",
        "digit_candidate": None,
        "text_count": 0,
    }

    if frame is None or getattr(frame, "size", 0) == 0:
        return None, debug

    # 优化：返回 slice 还是 copy?
    # 如果返回 copy，则增加内存。如果 slice，OCR 可能会再次 copy。
    # 这里我们只取 slice 传递给 subsequent steps.
    crop = crop_region_by_box(frame, crop_box=crop_box)
    if crop.size == 0:
        return None, debug

    debug["crop_shape"] = list(crop.shape)
    
    # --- 激进优化：仅使用单一最佳策略 ---
    # 不要生成多个变体循环尝试。对于 UID (小字体)，2x 放大通常是性价比最高的单一策略。
    # 原始尺寸往往太小导致 OCR 失败，而多重变体导致 CPU 爆炸。
    target_img = crop
    if cv2 is not None:
        try:
            # 针对小字体放大并填充边框，防止 OCR 检测框紧贴边缘被过滤
            target_img = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
            target_img = cv2.copyMakeBorder(
                target_img,
                top=20, bottom=20, left=20, right=20,
                borderType=cv2.BORDER_CONSTANT,
                value=[255, 255, 255]
            )
        except Exception:
            target_img = crop
    
    # 仅构建这一个列表
    # variants = [target_img] -- 变量也不用了
    debug["variant_count"] = 1

    all_texts: List[str] = []
    uid: Optional[str] = None
    
    # 仅运行一次 OCR
    # _ocr_texts 内部调用 recognize()，已包含位置检测和文本识别
    texts = _ocr_texts(ocr_engine, target_img)
    if texts:
        all_texts.extend(texts)
        uid = _extract_uid_from_texts(all_texts)

    debug.update(_collect_uid_debug(all_texts))
    debug["uid"] = uid
    return uid, debug
    debug["uid"] = uid
    return uid, debug


def detect_uid_value(
    frame: np.ndarray,
    ocr_engine,
    crop_box: Tuple[float, float, float, float] = DEFAULT_CROP_BOX,
) -> Optional[str]:
    uid, _ = detect_uid_value_with_debug(frame=frame, ocr_engine=ocr_engine, crop_box=crop_box)
    return uid


def detect_uid_bool(
    frame: np.ndarray,
    ocr_engine,
    crop_box: Tuple[float, float, float, float] = DEFAULT_CROP_BOX,
) -> bool:
    return detect_uid_value(frame=frame, ocr_engine=ocr_engine, crop_box=crop_box) is not None


def main():
    from src.ocr import OCR
    print("UID 特征码识别调试工具启动")
    worker = CaptureWorker(proc_name="Client-Win64-Shipping.exe", fps=5)
    worker.start()
    ocr = OCR(use_gpu=True)
    dumper = RecognitionDebugDumper(base_dir=str(runtime_logs_dir() / "recognition"))

    last_uid = None
    consistent_count = 0
    start = time.time()

    print("[UID] start detect ...")
    try:
        while time.time() - start < 60:
            frame = worker.frame
            if frame is None:
                time.sleep(0.3)
                continue

            crop = crop_region_by_box(frame)
            texts = _ocr_texts(ocr, crop)
            uid = detect_uid_value(frame=frame, ocr_engine=ocr)
            dumper.dump_uid(frame=frame, crop=crop, uid=uid, texts=texts)

            if uid:
                if uid == last_uid:
                    consistent_count += 1
                else:
                    last_uid = uid
                    consistent_count = 1
                print(f"[UID] candidate={uid} consistent={consistent_count}/3")
                if consistent_count >= 3:
                    print(f"[UID] locked={uid}")
                    break
            else:
                consistent_count = 0
                last_uid = None
                print("[UID] no match")

            time.sleep(0.8)
    finally:
        worker.stop()


if __name__ == "__main__":
    main()
