"""observation_extractor.py

从 OCR 文本与解析结果提取 EchoObservation，并管理词典热重载。
"""
from __future__ import annotations

import json
import os
import re
import time

from src.parser import parse_texts as _parse_texts, normalize_affix_name
from src.resources import writable_data_path
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class EchoObservation:
    level: Optional[int]
    cost: Optional[int]
    set_name: str
    main_stat: str
    echo_name: str
    equipment: Optional[str]
    is_locked: Optional[bool]
    substats: List[Dict[str, Any]]
    source_region: str
    ocr_confidence: float
    ui_mode: str
    slot_states: List[Dict[str, Any]]
    identity_candidate: Optional[Dict[str, Any]] = None


class ObservationExtractor:
    def __init__(
        self,
        echo_dictionary_path: str = "data/echo_dictionary.json",
        substat_values_path: str = "data/substat_values.json",
    ):
        self.echo_dictionary_path = writable_data_path(echo_dictionary_path)
        self.substat_values_path = writable_data_path(substat_values_path)
        self.echo_dictionary_reload_interval = 2.0
        self._next_echo_dictionary_reload_at = 0.0
        self._echo_dictionary_mtime = self._get_echo_dictionary_mtime()
        self.echo_dictionary = self._load_echo_dictionary()
        # 副属性数值配置
        self._substat_values_mtime = self._get_file_mtime(self.substat_values_path)
        self.substat_values: List[Dict[str, Any]] = self._load_substat_values()
        # 建立 (name, is_percent) → 配置条目 的查找索引
        self._substat_index: Dict[tuple, Dict[str, Any]] = self._build_substat_index()
        # 核心职责分层：
        # 1) 字典匹配（声骸/套装/主词条）
        # 2) 副词条区域切片与 OCR 文本拼接
        # 3) 副词条合法性过滤与档位判定
        # 4) 槽位状态构建（强化/调谐 UI 统一口径）
        # 声骸身份的跨页面防抖由 PipelineRunner 的 ActiveEchoContext 负责。

    def _load_echo_dictionary(self) -> Dict[str, List[str]]:
        default = {
            "set_names": [],
            "main_stats": [],
            "echo_names": [],
        }
        try:
            if not os.path.exists(self.echo_dictionary_path):
                return default
            with open(self.echo_dictionary_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return default
            out = dict(default)
            for key in out:
                raw = payload.get(key, [])
                if isinstance(raw, list):
                    out[key] = [str(x) for x in raw if str(x).strip()]
            return out
        except Exception:
            return default

    @staticmethod
    def _get_file_mtime(path: str) -> Optional[float]:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def _get_echo_dictionary_mtime(self) -> Optional[float]:
        return self._get_file_mtime(self.echo_dictionary_path)

    # ── 副属性数值配置加载 ─────────────────────────────────
    def _load_substat_values(self) -> List[Dict[str, Any]]:
        """从 substat_values.json 加载副属性数值配置列表。"""
        try:
            if not os.path.exists(self.substat_values_path):
                return []
            with open(self.substat_values_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw = payload.get("substats", [])
            if not isinstance(raw, list):
                return []
            return raw
        except Exception:
            return []

    def _build_substat_index(self) -> Dict[tuple, Dict[str, Any]]:
        """建立 (标准化名, is_percent) → 配置条目 的查找表，包括 alias 映射。"""
        idx: Dict[tuple, Dict[str, Any]] = {}
        for entry in self.substat_values:
            name = entry.get("name", "")
            is_pct = bool(entry.get("is_percent", False))
            idx[(name, is_pct)] = entry
            for alias in entry.get("alias", []):
                idx[(alias, is_pct)] = entry
        return idx

    def reload_echo_dictionary_if_needed(self, force: bool = False):
        now = time.time()
        if not force and now < self._next_echo_dictionary_reload_at:
            return
        self._next_echo_dictionary_reload_at = now + self.echo_dictionary_reload_interval

        # 声骸词典
        latest_mtime = self._get_echo_dictionary_mtime()
        if force or latest_mtime != self._echo_dictionary_mtime:
            self.echo_dictionary = self._load_echo_dictionary()
            self._echo_dictionary_mtime = latest_mtime

        # 副属性数值配置
        sv_mtime = self._get_file_mtime(self.substat_values_path)
        if force or sv_mtime != self._substat_values_mtime:
            self.substat_values = self._load_substat_values()
            self._substat_index = self._build_substat_index()
            self._substat_values_mtime = sv_mtime

    @staticmethod
    def normalize_match_text(text: str) -> str:
        # 移除空白和常见标点符号，使 "小翼龙·导电" 和 "小翼龙导电" 都能匹配
        return re.sub(r"[\s·•‧・\-]", "", text or "")

    @staticmethod
    def _empty_echo_attrs() -> Dict[str, Any]:
        return {
            "level": None,
            "cost": None,
            "set_name": "",
            "main_stat": "",
            "echo_name": "",
            "equipment": None,
        }

    @staticmethod
    def _is_empty_echo_attrs(attrs: Dict[str, Any]) -> bool:
        return not any(
            str(attrs.get(key) or "").strip()
            for key in ("set_name", "main_stat", "echo_name", "equipment")
        )

    @classmethod
    def _normalize_echo_attrs(
        cls,
        level: Optional[int],
        cost: Optional[int],
        set_name: str,
        main_stat: str,
        echo_name: str,
        equipment: Optional[str],
    ) -> Dict[str, Any]:
        attrs = {
            "level": level,
            "cost": cost,
            "set_name": "" if set_name == "未知套装" else str(set_name or "").strip(),
            "main_stat": "" if main_stat == "未知主词条" else str(main_stat or "").strip(),
            "echo_name": "" if echo_name == "未知声骸" else str(echo_name or "").strip(),
            "equipment": str(equipment or "").strip() or None,
        }
        if cls._is_empty_echo_attrs(attrs):
            return cls._empty_echo_attrs()
        return attrs

    def match_by_dictionary(self, text: str, dictionary_key: str, fallback: str) -> str:
        candidates = self.echo_dictionary.get(dictionary_key, [])
        # 按名称长度降序排列，避免短名（如"角"）匹配到无关文本（如"角色"）
        sorted_candidates = sorted(candidates, key=len, reverse=True)
        norm_text = self.normalize_match_text(text)
        
        # pass 1: 精确子串命中 (字典词在文本中)
        for name in sorted_candidates:
            norm_name = self.normalize_match_text(name)
            # 对于单字声骸（如“角”），不能放任其变成子串匹配（否则会匹配到如“角色”包含角）
            # 所以对单字名称要求：要么完全相等，要么至少被识别成了孤立的信息（这里最安全的是要求原文本长度也很短）
            if len(norm_name) < 2:
                # 只有当OCR出的文本（剥离杂质后）也就只有一个字，或者确实原封不动就是这个单字时，才认定是它
                if len(norm_text) <= 2 and norm_name in norm_text:
                    return name
                continue
            if norm_name in norm_text:
                return name
                
        # pass 2: 容错子串反向匹配，针对 OCR 漏掉开头的情况
        # 比如 OCR 把 `湮灭伤害加成` 识别成了 `灭伤害加成`
        # 此时要求被识别的文本(去除数字/标点后)必须至少有3个字符，且被包含在字典词中
        text_no_digits = re.sub(r'[\d\.%％+＋]', '', norm_text)
        if len(text_no_digits) >= 3:
            for name in sorted_candidates:
                norm_name = self.normalize_match_text(name)
                # 检查该属性名是否包含了这段错误识别的残缺文本
                if text_no_digits in norm_name:
                    return name
                    
        return fallback

    # ── 副属性校验 / 档位判定 ────────────────────────────────
    def lookup_substat(self, name: str, is_pct: bool) -> Optional[Dict[str, Any]]:
        """根据标准化名和百分比标记查找副属性配置条目。"""
        return self._substat_index.get((name, is_pct))

    def classify_substat_tier(
        self, name: str, value: float, is_pct: bool, cost: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """返回副属性的档位信息：tier(1-based), quality('low'/'mid'/'high'), matched_value。
        若未查到配置或无法匹配，返回 None。"""
        entry = self.lookup_substat(name, is_pct)
        if not entry:
            return None
        vals = entry.get("values", [])
        if not vals or not isinstance(vals, list) or len(vals) == 0:
            return None

        # 找最接近的离散值：当前配置按离散档位建模，而非连续区间。
        best_idx = 0
        best_diff = abs(value - vals[0])
        for i, v in enumerate(vals[1:], 1):
            diff = abs(value - v)
            if diff < best_diff:
                best_diff = diff
                best_idx = i

        tier = best_idx + 1  # 1-based
        total = len(vals)
        if tier <= total * 0.33:
            quality = "low"
        elif tier <= total * 0.66:
            quality = "mid"
        else:
            quality = "high"

        return {
            "tier": tier,
            "total_tiers": total,
            "quality": quality,
            "matched_value": vals[best_idx],
            "diff": round(best_diff, 2),
        }

    def enrich_substats(
        self, parsed: List[Dict[str, Any]], cost: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """为 parser 输出的副属性列表附加档位信息（原地修改并返回）。"""
        for sub in parsed:
            tier_info = self.classify_substat_tier(
                name=sub.get("name", ""),
                value=sub.get("value", 0),
                is_pct=sub.get("is_pct", False),
                cost=cost,
            )
            if tier_info:
                sub["tier"] = tier_info["tier"]
                sub["total_tiers"] = tier_info["total_tiers"]
                sub["quality"] = tier_info["quality"]
                sub["matched_value"] = tier_info["matched_value"]
        return parsed

    # ── 副词条文本合并与过滤 ─────────────────────────────────
    _BULLETS = {"·", "•", "・", "‧"}
    _VAL_ONLY_RE = re.compile(r"^[+＋]?\s*[\d]+(?:\.[\d]+)?\s*[%％]?$")

    @classmethod
    def _extract_substat_section(cls, raw_texts: List[str]) -> List[str]:
        """从 OCR 文本列表中提取副词条区域。
        根据新的严格 10 行规则，传入的已经是限定为副词条的区域（5-9行），
        只需要过滤掉 '声骸技能', '待调谐', '合鸣效果' 之后的就行。
        """
        end_markers = ("声骸技能", "待调谐", "合鸣效果")
        end_idx = len(raw_texts)
        for i, t in enumerate(raw_texts):
            if any(m in t for m in end_markers):
                end_idx = i
                break
        
        return raw_texts[:end_idx]

    @classmethod
    def _merge_adjacent_ocr_texts(cls, raw_texts: List[str]) -> List[str]:
        """合并被 OCR 拆分的文本对。

        Pass 1: 独立的 · 号与下一元素合并。
        Pass 2: 词缀名(无数字) + 纯数值 合并。
        例: ['·', '生命', '8.6%'] → ['· 生命 8.6%']
        例: ['生命', '8.6%'] → ['生命 8.6%']
        """
        # Pass 1: bullet merge
        step1: List[str] = []
        i = 0
        while i < len(raw_texts):
            t = raw_texts[i].strip()
            if t in cls._BULLETS and i + 1 < len(raw_texts):
                step1.append(f"· {raw_texts[i + 1].strip()}")
                i += 2
            else:
                step1.append(raw_texts[i])
                i += 1

        # Pass 2: affix name + value merge
        #   在副词条区域内，名称和数值分开时合并
        merged: List[str] = []
        i = 0
        while i < len(step1):
            cur = step1[i]
            cur_clean = re.sub(r"[\s·•‧・]", "", cur)
            norm = normalize_affix_name(cur)
            is_affix_name = (norm != cur_clean) and not re.search(r"\d", cur)

            if (
                is_affix_name
                and i + 1 < len(step1)
                and cls._VAL_ONLY_RE.match(step1[i + 1].strip())
            ):
                merged.append(f"{cur} {step1[i + 1].strip()}")
                i += 2
            else:
                merged.append(cur)
                i += 1
        return merged

    def _filter_valid_substats(
        self,
        parsed: List[Dict[str, Any]],
        level: Optional[int],
        main_stat: str = "",
    ) -> List[Dict[str, Any]]:
        """只保留匹配已知副属性、数值在合理范围内的条目，并按等级限制数量。
        对未识别名称但数值在已知副属性范围内的孤立值，尝试推断名称。"""
        # 等级上限约束：+5/+10/+15/+20/+25 最多对应 1~5 条副词条。
        # 强化页可能不显示等级，此时以屏幕上可见的槽位文本为准。
        if level is None:
            max_subs = 5
        else:
            max_subs = (level // 5) if level >= 5 else 0
        if max_subs <= 0:
            return []

        result: List[Dict[str, Any]] = []
        used_names: set = set()  # 跟踪已用的副属性名，避免重复推断

        for p in parsed:
            name = p.get("name", "")
            value = p.get("value", 0)
            is_pct = p.get("is_pct", False)

            # 名称匹配已知副属性
            entry = self.lookup_substat(name, is_pct)

            if entry is None:
                # 名称不匹配 → 尝试通过数值推断副属性名
                # 例: OCR 漏掉 "· 生命"，只留下 "8.6%"
                inferred = self._infer_substat_by_value(value, is_pct, used_names)
                if inferred is None:
                    continue
                p = dict(p)  # 不修改原始 dict
                p["name"] = inferred["name"]
                entry = inferred["entry"]

            # 数值需在副属性合理范围内（允许 OCR 误差容忍）
            vals = entry.get("values", [])
            if vals:
                min_val = min(vals)
                max_val = max(vals)
                margin = max((max_val - min_val) * 0.15, 0.5)
                if value < min_val - margin or value > max_val + margin:
                    continue

            used_names.add((p["name"], is_pct))
            result.append(p)
        return result[:max_subs]

    def _infer_substat_by_value(
        self, value: float, is_pct: bool, exclude: set
    ) -> Optional[Dict[str, Any]]:
        """根据数值和百分比标记，从已知副属性中推断最可能的名称。
        仅当恰好只有一个候选匹配时才返回结果（避免歧义）。"""
        candidates = []
        for entry in self.substat_values:
            if bool(entry.get("is_percent", False)) != is_pct:
                continue
            name = entry.get("name", "")
            if (name, is_pct) in exclude:
                continue
            vals = entry.get("values", [])
            if not vals:
                continue
            min_v, max_v = min(vals), max(vals)
            margin = max((max_v - min_v) * 0.15, 0.5)
            if min_v - margin <= value <= max_v + margin:
                candidates.append({"name": name, "entry": entry})

        # 百分比副属性值域重叠较多，这里采用“首个候选”启发式。
        # 如需更稳妥，可后续引入上下文（套装、主词条、历史分布）进行排序。
        if len(candidates) >= 1:
            return candidates[0]
        return None

    def _extract_substats(
        self,
        raw_texts: List[str],
        level: Optional[int],
        cost: Optional[int],
        main_stat: str = "",
    ) -> List[Dict[str, Any]]:
        """从 OCR 原始文本中提取副词条：
        1. 用区域标记截取副词条文本段
        2. 合并拆分文本
        3. 解析 → 验证 → 附加档位
        """
        section = self._extract_substat_section(raw_texts)
        merged = self._merge_adjacent_ocr_texts(section)
        parsed = _parse_texts(merged)
        filtered = self._filter_valid_substats(parsed, level, main_stat=main_stat)
        enriched = self.enrich_substats(filtered, cost=cost)
        return enriched

    def _extract_single_substat_from_row(
        self,
        row_text: str,
        cost: Optional[int],
        main_stat: str = "",
    ) -> Optional[Dict[str, Any]]:
        """从单行副词条文本中提取一个有效副词条，失败返回 None。"""
        text = (row_text or "").strip()
        if not text or "待调谐" in text:
            return None

        parsed = _parse_texts([text])
        if not parsed:
            return None

        filtered = self._filter_valid_substats(parsed, level=25, main_stat=main_stat)
        if not filtered:
            return None

        enriched = self.enrich_substats(filtered, cost=cost)
        if not enriched:
            return None
        return enriched[0]

    @staticmethod
    def slot_threshold(slot_index: int) -> int:
        thresholds = [5, 10, 15, 20, 25]
        if slot_index < 1:
            return 5
        if slot_index > len(thresholds):
            return thresholds[-1]
        return thresholds[slot_index - 1]

    @staticmethod
    def infer_ui_mode(raw_texts: List[str]) -> str:
        return "enhance_panel"

    @classmethod
    def extract_equipment(cls, raw_texts: List[str]) -> Optional[str]:
        for row in raw_texts:
            text = str(row or "").strip()
            if "装配中" not in text:
                continue
            equipment = text.replace("装配中", "").strip()
            equipment = re.sub(r"^[\s:：\-—]+|[\s:：\-—]+$", "", equipment)
            return equipment or None
        return None

    def build_slot_states(
        self,
        level: Optional[int],
        activated_substats: List[Dict[str, Any]],
        ui_mode: str,
        raw_texts: List[str] = None,
        cost: Optional[int] = None,
        main_stat: str = "",
    ) -> List[Dict[str, Any]]:
        if raw_texts is None:
            raw_texts = []
            
        current_level = level if level is not None else 0
        states: List[Dict[str, Any]] = []

        slot_start = 4
        if ui_mode == "enhance_panel":
            slot_start = self._enhance_panel_layout(raw_texts)["slot_start"]

        # 规则：详情页 Row5-Row9 对应辅音1-5；强化页根据标题行是否被 OCR 漏掉动态定位。
        slot_texts = raw_texts[slot_start:slot_start + 5] if len(raw_texts) > slot_start else []

        # 优先按固定行位解析每个槽位，避免“按解析顺序前移”导致槽位错乱
        row_substats: Dict[int, Dict[str, Any]] = {}
        for slot_index in range(1, 6):
            slot_raw = slot_texts[slot_index - 1] if slot_index - 1 < len(slot_texts) else ""
            sub = self._extract_single_substat_from_row(slot_raw, cost=cost, main_stat=main_stat)
            if sub is not None:
                row_substats[slot_index] = sub

        # 兼容旧路径：当无 raw 行信息时，回退使用已激活副词条的顺序映射
        fallback_seq_substats: Dict[int, Dict[str, Any]] = {}
        if not row_substats and activated_substats:
            for idx, sub in enumerate(activated_substats[:5], start=1):
                fallback_seq_substats[idx] = sub

        unlocked_slots = max(0, min(5, current_level // 5))
        current_tunable_assigned = False

        for slot_index in range(1, 6):
            threshold = self.slot_threshold(slot_index)
            slot_raw = slot_texts[slot_index - 1].strip() if slot_index - 1 < len(slot_texts) else ""
            
            sub = row_substats.get(slot_index) or fallback_seq_substats.get(slot_index)
            if sub is not None:
                val = sub.get("value")
                value_text = f"{val}%" if sub.get("is_pct") else str(val)
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "activated",
                        "text": f"{sub.get('name', '未知副词条')} {value_text}",
                        "name": sub.get("name"),
                        "value": sub.get("value"),
                        "is_pct": sub.get("is_pct"),
                        "quality": sub.get("quality"),
                    }
                )
                continue

            if "激活新辅音属性" in slot_raw:
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "current_tunable",
                        "text": "激活新辅音属性",
                        "name": None,
                        "value": None,
                        "is_pct": None,
                        "quality": None,
                    }
                )
                current_tunable_assigned = True
                continue

            if "待调谐" in slot_raw:
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "pending_tunable",
                        "text": "待调谐",
                        "name": None,
                        "value": None,
                        "is_pct": None,
                        "quality": None,
                    }
                )
                continue

            locked_match = re.search(r"强化至\s*\+?\s*(\d{1,2})\s*可调谐", slot_raw)
            if locked_match:
                try:
                    threshold = int(locked_match.group(1))
                except ValueError:
                    threshold = self.slot_threshold(slot_index)
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "locked_by_level",
                        "text": f"强化至+{threshold}可调谐",
                        "name": None,
                        "value": None,
                        "is_pct": None,
                        "quality": None,
                    }
                )
                continue

            # 没有显式槽位文本时再根据等级推断锁定状态。
            if level is not None and current_level < threshold:
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "locked_by_level",
                        "text": f"强化至+{threshold}可调谐",
                    }
                )
                continue

            if slot_index <= unlocked_slots:
                states.append(
                    {
                        "slot_index": slot_index,
                        "threshold": threshold,
                        "status": "pending_tunable",
                        "text": "待调谐",
                        "name": None,
                        "value": None,
                        "is_pct": None,
                        "quality": None,
                    }
                )
                continue

            states.append(
                {
                    "slot_index": slot_index,
                    "threshold": threshold,
                    "status": "locked_by_level" if level is not None else "pending_tunable",
                    "text": f"强化至+{threshold}可调谐" if level is not None else "待调谐",
                    "name": None,
                    "value": None,
                    "is_pct": None,
                    "quality": None,
                }
            )

        return states

    def _find_dictionary_row_index(
        self,
        raw_texts: List[str],
        dictionary_key: str,
        fallback: str,
        start: int = 0,
        end: Optional[int] = None,
    ) -> Optional[int]:
        end = len(raw_texts) if end is None else min(end, len(raw_texts))
        for idx in range(max(0, start), end):
            if self.match_by_dictionary(raw_texts[idx], dictionary_key, fallback) != fallback:
                return idx
        return None

    @staticmethod
    def _looks_like_main_stat_row(text: str) -> bool:
        row = str(text or "").strip()
        if not row:
            return False
        if re.search(r"^\s*[xX]?\s*(攻击|生命|防御)\s*[:：]?\s*\d+(?:\.\d+)?\s*$", row):
            return False
        if "%" in row or "％" in row:
            return True
        return any(marker in row for marker in ("伤害加成", "暴击", "共鸣效率", "治疗效果加成"))

    def _enhance_panel_layout(self, raw_texts: List[str]) -> Dict[str, int]:
        top_end = min(len(raw_texts), 5)
        name_idx = self._find_dictionary_row_index(
            raw_texts,
            "echo_names",
            "未知声骸",
            start=0,
            end=top_end,
        )
        if name_idx is None:
            title = raw_texts[0] if raw_texts else ""
            name_idx = 1 if len(raw_texts) > 1 and any(marker in title for marker in ("声骸", "调谐", "强化")) else 0

        main_idx = self._find_dictionary_row_index(
            raw_texts,
            "main_stats",
            "未知主词条",
            start=name_idx + 1,
            end=min(len(raw_texts), name_idx + 4),
        )
        if main_idx is None:
            main_idx = min(name_idx + 1, max(0, len(raw_texts) - 1))

        expected_main_idx = name_idx + 1
        if expected_main_idx < len(raw_texts) and self._looks_like_main_stat_row(raw_texts[expected_main_idx]):
            main_idx = expected_main_idx

        return {
            "name_idx": name_idx,
            "main_idx": main_idx,
            "slot_start": min(main_idx + 2, len(raw_texts)),
        }

    def extract_observation(
        self,
        raw_texts: List[str],
        parsed: List[Dict[str, Any]],
        conf: float,
        ui_mode_override: Optional[str] = None,
    ) -> EchoObservation:
        def get_row(idx: int) -> str:
            return raw_texts[idx] if idx < len(raw_texts) else ""

        text = " ".join(raw_texts)
        ui_mode = ui_mode_override or self.infer_ui_mode(raw_texts)

        if ui_mode == "enhance_panel":
            layout = self._enhance_panel_layout(raw_texts)
            row_name = get_row(layout["name_idx"])
            row_cost = ""
            row_level = ""
            row_main = get_row(layout["main_idx"])
            row_sub = get_row(layout["main_idx"] + 1)
        else:
            row_name = get_row(0)
            row_cost = get_row(1)
            row_level = get_row(2)
            # 主词条固定为 Row3（索引 2）。
            row_main = get_row(2)
            # Row4 作为主词条补百分号的参考行。
            row_sub = get_row(3)

        level = None
        level_candidates: List[int] = []
        # 优先从 Row1 识别等级（例如："异相·辛吉勒姆 +25"）
        for m in re.finditer(r"\+(\d{1,2})", row_name):
            try:
                lv = int(m.group(1))
                if 0 <= lv <= 25:
                    level_candidates.append(lv)
            except:
                pass
        if not level_candidates:
            for m in re.finditer(r"\+(\d{1,2})", row_level):
                try:
                    lv = int(m.group(1))
                    if 0 <= lv <= 25:
                        level_candidates.append(lv)
                except:
                    pass
        if not level_candidates and ui_mode != "enhance_panel":
            for m in re.finditer(r"\+(\d{1,2})", text):
                try:
                    lv = int(m.group(1))
                    if 0 <= lv <= 25:
                        level_candidates.append(lv)
                except:
                    pass
        if level_candidates:
            level = max(level_candidates)

        cost = None
        m_cost = re.search(r"([134])", row_cost)
        if not m_cost:
             m_cost = re.search(r"COST\s*[:：]?\s*([134])", text, flags=re.IGNORECASE)
        if m_cost:
            cost = int(m_cost.group(1))

        is_locked = None
        if "锁定" in text:
            is_locked = True
        elif "未锁定" in text:
            is_locked = False

        equipment = self.extract_equipment(raw_texts)
        
        set_name = "未知套装"
        # 优先在剩下的行 (10行之后) 匹配套装
        for row in raw_texts[10:]:
            match_res = self.match_by_dictionary(row, "set_names", "未知套装")
            if match_res != "未知套装":
                set_name = match_res
                break
        
        # 兜底：如果没匹配到，由于可能存在OCR少识别空行的情况，在前面也找一下
        if set_name == "未知套装":
            for row in raw_texts[:10]:
                match_res = self.match_by_dictionary(row, "set_names", "未知套装")
                if match_res != "未知套装":
                    set_name = match_res
                    break

        # 仅在 row_main (OCR第3行) 中匹配主词条，绝不向下(row_sub)或全文寻找
        # 以此彻底避免将第5行开始的辅音属性误认为主属性
        # 首先利用已知词缀纠错系统（它包含了像“行射”纠错为“衍射”等常见的OCR误读处理）
        norm_row_main = normalize_affix_name(row_main)
        main_stat = self.match_by_dictionary(norm_row_main, "main_stats", "未知主词条")
        
        # 兜底：如果标准化匹配由于某种原因落空了，再用原始行回退测一次
        if main_stat == "未知主词条":
            main_stat = self.match_by_dictionary(row_main, "main_stats", "未知主词条")
        if main_stat == "未知主词条":
            # OCR 行数会因漏识别而前后漂移；只在面板顶部 Row1~Row4 兜底，避免把辅音行误判成主词条。
            for candidate_row in raw_texts[:4]:
                row = (candidate_row or "").strip()
                if not row or re.fullmatch(r"(?:COST\s*)?[+:]?\s*\d{1,2}%?", row, flags=re.IGNORECASE):
                    continue
                normalized = normalize_affix_name(row)
                match_res = self.match_by_dictionary(normalized, "main_stats", "未知主词条")
                if match_res == "未知主词条":
                    match_res = self.match_by_dictionary(row, "main_stats", "未知主词条")
                if match_res != "未知主词条":
                    main_stat = match_res
                    break

        # 行检测加上识别百分比%的功能
        if main_stat in ["攻击", "生命", "防御"]:
            # 在匹配到基础三维时，我们依旧允许把 row_sub 加进来寻找 "%"
            # 因为 OCR 虽然把名字读在了 row_main，但有可能把 "30.0%" 这个纯数字折行到了 row_sub
            val_search_space = row_main + " " + row_sub

            if "%" in val_search_space or "％" in val_search_space:
                main_stat += "%"

        echo_name = self.match_by_dictionary(row_name, "echo_names", "未知声骸")
        if echo_name == "未知声骸":
            echo_name = self.match_by_dictionary(text, "echo_names", "未知声骸")

        identity_candidate = self._normalize_echo_attrs(
            level=level,
            cost=cost,
            set_name=set_name,
            main_stat=main_stat,
            echo_name=echo_name,
            equipment=equipment,
        )
        level = identity_candidate["level"]
        cost = identity_candidate["cost"]
        set_name = identity_candidate["set_name"]
        main_stat = identity_candidate["main_stat"]
        echo_name = identity_candidate["echo_name"]
        equipment = identity_candidate["equipment"]

        slot_start = 4
        if ui_mode == "enhance_panel":
            slot_start = self._enhance_panel_layout(raw_texts)["slot_start"]
        # 严格限定副词条为 5 个槽位行。
        substats_region = raw_texts[slot_start:slot_start + 5] if len(raw_texts) > slot_start else []
        substats = self._extract_substats(substats_region, level, cost, main_stat=main_stat)
        slot_states = self.build_slot_states(
            level=level,
            activated_substats=substats,
            ui_mode=ui_mode,
            raw_texts=raw_texts,
            cost=cost,
            main_stat=main_stat,
        )
        print(
            f"[ObservationExtractor]\n"
            f"声骸名: {echo_name}\n"
            f"cost: {cost}\n"
            f"等级: {level}\n"
            f"套装名: {set_name}\n"
            f"主词条: {main_stat}\n"
            f"装配角色: {equipment or '-'}\n"
            f"副属性:\n" + "\n".join([str(s) for s in substats]) + "\n"
            f"ui_mode: {ui_mode}\n"
            f"锁定: {is_locked}"
        )
        return EchoObservation(
            level=level,
            cost=cost,
            set_name=set_name,
            main_stat=main_stat,
            echo_name=echo_name,
            equipment=equipment,
            is_locked=is_locked,
            substats=substats,
            source_region="panel_center",
            ocr_confidence=float(conf or 0.0),
            ui_mode=ui_mode,
            slot_states=slot_states,
            identity_candidate=identity_candidate,
        )
