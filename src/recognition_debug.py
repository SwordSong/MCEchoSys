"""recognition_debug.py

统一将识别相关截图输出到 outputs 目录。
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

try:
    import cv2
except Exception:
    cv2 = None


class RecognitionDebugDumper:
    def __init__(self, base_dir: str = "outputs/recognition"):
        self.base_dir = base_dir
        self.uid_dir = os.path.join(base_dir, "uid")
        self.panel_dir = os.path.join(base_dir, "panel")
        os.makedirs(self.uid_dir, exist_ok=True)
        os.makedirs(self.panel_dir, exist_ok=True)

    def _write_image(self, path: str, image) -> None:
        if cv2 is None or image is None:
            return
        try:
            cv2.imwrite(path, image)
        except Exception:
            pass

    def _write_text(self, path: str, lines: Iterable[str]) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                for line in lines:
                    f.write(str(line) + "\n")
        except Exception:
            pass

    def dump_uid(self, frame, crop, uid: Optional[str], texts: Iterable[str]) -> None:
        self._write_image(os.path.join(self.uid_dir, "frame_latest.png"), frame)
        self._write_image(os.path.join(self.uid_dir, "crop_latest.png"), crop)
        self._write_text(
            os.path.join(self.uid_dir, "ocr_latest.txt"),
            [f"uid={uid or ''}", *list(texts)],
        )

    def dump_panel(self, frame, crop, processed, det_index: int, raw_texts: Iterable[str]) -> None:
        self._write_image(os.path.join(self.panel_dir, "frame_latest.png"), frame)
        self._write_image(os.path.join(self.panel_dir, f"det_{det_index}_crop_latest.png"), crop)
        self._write_image(os.path.join(self.panel_dir, f"det_{det_index}_processed_latest.png"), processed)
        self._write_text(
            os.path.join(self.panel_dir, f"det_{det_index}_ocr_latest.txt"),
            list(raw_texts),
        )
