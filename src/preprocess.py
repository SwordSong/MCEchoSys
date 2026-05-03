"""
preprocess.py — 图像预处理模块
负责将截图画面中的目标区域裁剪并进行二值化、超分辨率放大等图像增强处理，
以消除背景干扰，从而大幅提升后续 PaddleOCR 的文字识别准确度。
即便在没有安装 OpenCV (cv2) 的极简环境下，也提供了基于 Numpy 的轻量级后备方案供单元测试使用。
"""
try:
    import cv2
except Exception:
    cv2 = None

import numpy as np
from typing import Tuple, List


# --- Lightweight fallbacks -------------------------------------------------
def _to_gray_numpy(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return (0.299 * img[..., 2] + 0.587 * img[..., 1] + 0.114 * img[..., 0]).astype(np.uint8)
    return img


def _gaussian_blur_numpy(img: np.ndarray, ksize=(3, 3)) -> np.ndarray:
    # very small-box blur as fallback using uniform kernel
    ky, kx = ksize
    pad_y = ky // 2
    pad_x = kx // 2
    padded = np.pad(img, ((pad_y, pad_y), (pad_x, pad_x)), mode="reflect")
    out = np.empty_like(img)
    for y in range(img.shape[0]):
        for x in range(img.shape[1]):
            out[y, x] = padded[y : y + ky, x : x + kx].mean()
    return out.astype(np.uint8)


def _adaptive_threshold_numpy(img: np.ndarray, blockSize=11, C=2):
    # simple local mean threshold as fallback
    assert blockSize % 2 == 1 and blockSize >= 3
    pad = blockSize // 2
    padded = np.pad(img, pad, mode="reflect")
    out = np.zeros_like(img, dtype=np.uint8)
    for y in range(img.shape[0]):
        for x in range(img.shape[1]):
            local = padded[y : y + blockSize, x : x + blockSize]
            thresh = local.mean() - C
            out[y, x] = 255 if img[y, x] > thresh else 0
    return out


def _resize_numpy(img: np.ndarray, fx=1.0, fy=1.0):
    if fx == 1 and fy == 1:
        return img
    ny = int(round(img.shape[0] * fy))
    nx = int(round(img.shape[1] * fx))
    # nearest-neighbor upscale/downscale
    y_idx = (np.linspace(0, img.shape[0] - 1, ny)).astype(int)
    x_idx = (np.linspace(0, img.shape[1] - 1, nx)).astype(int)
    return img[np.ix_(y_idx, x_idx, range(img.shape[2]) if img.ndim == 3 else [])]


# --- API -------------------------------------------------------------------


def crop_bbox(img: np.ndarray, bbox) -> np.ndarray:
    """
    根据给定的边框坐标 (Bounding Box) 对图像进行安全裁剪，防止坐标越界。
    :param img: 输入图像 (numpy array)
    :param bbox: 坐标列表 [x1, y1, x2, y2]
    :return: 裁剪后的局部图像
    """
    x1, y1, x2, y2 = map(int, bbox)
    h, w = img.shape[:2]
    x1 = max(0, min(x1, w - 1))
    x2 = max(0, min(x2, w))
    y2 = max(0, min(y2, h))
    return img[y1:y2, x1:x2]


# 图像增强函数：将裁剪后的图片转为黑白分明、背景干净的二值图，对后续的 OCR 引擎更友好。
# 处理流程：转灰度图 -> 高斯模糊(降噪) -> 局部自适应阈值二值化(抹掉渐变背景) -> 形态学闭运算修复断裂边缘
def enhance_for_ocr(img: np.ndarray) -> np.ndarray:
    """将裁剪后的图片转为 OCR 友好的二值图。"""
    if img.size == 0:
        return img
    if cv2 is not None:
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        th = cv2.adaptiveThreshold(
            blur, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=11,
            C=2,
        )
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel)
        return th
    else:
        # 无 OpenCV 时的降级方案
        gray = _to_gray_numpy(img) if img.ndim == 3 else img
        blur = _gaussian_blur_numpy(gray, (3, 3))
        th = _adaptive_threshold_numpy(blur, blockSize=11, C=2)
        return th


# 超分辨率适配：鉴于 OCR 引擎对小字体的识别率较低且易受噪点影响，
# 当检测到输入的裁剪块高度小于基准阈值 (默认 48 像素) 时，自动对其进行放大。
def upscale_if_small(img: np.ndarray, min_height: int = 48) -> np.ndarray:
    """若图片高度太小，放大以提高 OCR 准确率。"""
    h = img.shape[0]
    if h < min_height:
        scale = min_height / h
        if cv2 is not None:
            # INTER_CUBIC 这种高清插值算法对文字边缘的平滑效果最好
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        else:
            img = _resize_numpy(img, fx=scale, fy=scale)
    return img


# 预处理流水线总入口；依次应用三大预处理工序：
# 1. crop: 根据 YOLO 边界框截取小图
# 2. upscale: 画面过小则自动高清放大
# 3. enhance: 自适应极化增强去背景
# 返回处理好的黑白掩膜图片供 OCR 读取。
def preprocess_pipeline(img: np.ndarray, bbox) -> np.ndarray:
    """一步到位：裁剪 → 放大 → 增强。"""
    crop = crop_bbox(img, bbox)
    if crop.size == 0:
        return crop
    crop = upscale_if_small(crop)
    return enhance_for_ocr(crop)
