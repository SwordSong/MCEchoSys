"""strategy_config.py — 套装副词条优先级配置加载器。

用户可通过 JSON 配置副词条优先级：
- 全局默认优先级
- 按套装覆盖优先级
- 平级组（equal_groups）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

from src.resources import writable_data_path


DEFAULT_CONFIG = {
    "version": 1,
    "weights": {
        "high": 1.0,
        "medium": 0.7,
        "low": 0.4,
        "fallback": 0.5,
    },
    "default_priorities": {
        "high": ["暴击", "暴击伤害"],
        "medium": ["攻击", "共鸣效率"],
        "low": ["防御", "生命"],
        "equal_groups": [["暴击", "暴击伤害"]],
    },
    "set_priorities": {},
}


@dataclass
class StrategyPriorityProfile:
    fallback_weight: float
    weights_by_level: Dict[str, float]
    default_weights: Dict[str, float]
    set_weights: Dict[str, Dict[str, float]]
    set_character_weights: Dict[str, Dict[str, Dict[str, float]]]
    set_default_character: Dict[str, str]
    set_meta: Dict[str, Dict[str, Any]]
    set_character_meta: Dict[str, Dict[str, Dict[str, Any]]]

    def available_characters_for_set(self, set_name: str | None) -> List[str]:
        if not set_name:
            return []
        role_map = self.set_character_weights.get(set_name, {})
        return list(role_map.keys())

    def resolve_character_for_set(self, set_name: str | None, preferred: str | None = None) -> Optional[str]:
        if not set_name:
            return None
        role_map = self.set_character_weights.get(set_name, {})
        if not role_map:
            return None

        if preferred and preferred in role_map:
            return preferred

        configured_default = self.set_default_character.get(set_name)
        if configured_default and configured_default in role_map:
            return configured_default

        for role_name in role_map.keys():
            return role_name
        return None

    def weights_for(
        self,
        set_name: str | None = None,
        character_name: str | None = None,
    ) -> Tuple[Dict[str, float], str, Optional[str], List[str]]:
        if set_name:
            role_map = self.set_character_weights.get(set_name, {})
            available = list(role_map.keys())
            if role_map:
                resolved = self.resolve_character_for_set(set_name, preferred=character_name)
                if resolved and resolved in role_map:
                    source = "set_role_override" if character_name and character_name == resolved else "set_role_default"
                    return role_map[resolved], source, resolved, available

            if set_name in self.set_weights:
                return self.set_weights[set_name], "set", None, []

        return self.default_weights, "default", None, []

    def weight_for(
        self,
        substat_name: str,
        set_name: str | None = None,
        character_name: str | None = None,
    ) -> float:
        mapping, _, _, _ = self.weights_for(set_name=set_name, character_name=character_name)
        return mapping.get(substat_name, self.fallback_weight)

    def perfect_for(
        self,
        set_name: str | None = None,
        character_name: str | None = None,
    ) -> Tuple[Dict[str, Any], str, Optional[str], List[str]]:
        if set_name:
            role_map = self.set_character_meta.get(set_name, {})
            available = self.available_characters_for_set(set_name) or list(role_map.keys())
            if role_map:
                resolved = self.resolve_character_for_set(set_name, preferred=character_name)
                if not resolved or resolved not in role_map:
                    if character_name and character_name in role_map:
                        resolved = character_name
                    elif self.set_default_character.get(set_name) in role_map:
                        resolved = self.set_default_character.get(set_name)
                    else:
                        resolved = next(iter(role_map.keys()))

                if resolved and resolved in role_map:
                    source = "set_role_override" if character_name and character_name == resolved else "set_role_default"
                    return role_map[resolved], source, resolved, available

            set_meta = self.set_meta.get(set_name, {})
            if set_meta:
                return set_meta, "set", None, []

        return {}, "default", None, []


@dataclass
class StrategyConfigLoadResult:
    profile: StrategyPriorityProfile
    used_default: bool
    errors: List[str]


def _build_weight_map(priorities: Dict[str, Any], level_weights: Dict[str, float], fallback_weight: float) -> Dict[str, float]:
    mapping: Dict[str, float] = {}
    for level_name in ("high", "medium", "low"):
        names = priorities.get(level_name, []) or []
        weight = float(level_weights.get(level_name, fallback_weight))
        for n in names:
            mapping[str(n)] = weight

    equal_groups: List[List[str]] = priorities.get("equal_groups", []) or []
    for group in equal_groups:
        if not group:
            continue
        current = [mapping.get(str(n), fallback_weight) for n in group]
        same_weight = max(current) if current else fallback_weight
        for n in group:
            mapping[str(n)] = float(same_weight)
    return mapping


def _ensure_dict(value: Any, name: str, errors: List[str]) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    errors.append(f"{name} 必须是对象，已回退默认")
    return {}


def _ensure_list_of_str(value: Any, name: str, errors: List[str]) -> List[str]:
    if not isinstance(value, list):
        errors.append(f"{name} 必须是字符串数组，已忽略")
        return []
    out = []
    for idx, item in enumerate(value):
        if isinstance(item, str):
            out.append(item)
        else:
            errors.append(f"{name}[{idx}] 不是字符串，已忽略")
    return out


def _ensure_equal_groups(value: Any, name: str, errors: List[str]) -> List[List[str]]:
    if not isinstance(value, list):
        errors.append(f"{name} 必须是二维字符串数组，已忽略")
        return []
    groups: List[List[str]] = []
    for g_idx, group in enumerate(value):
        if not isinstance(group, list):
            errors.append(f"{name}[{g_idx}] 不是数组，已忽略")
            continue
        cleaned = []
        for i_idx, item in enumerate(group):
            if isinstance(item, str):
                cleaned.append(item)
            else:
                errors.append(f"{name}[{g_idx}][{i_idx}] 不是字符串，已忽略")
        if cleaned:
            groups.append(cleaned)
    return groups


def _safe_float(value: Any, default: float, name: str, errors: List[str], min_value: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        errors.append(f"{name} 不是数字，已回退默认 {default}")
        return default
    if parsed < min_value:
        errors.append(f"{name} 小于 {min_value}，已回退默认 {default}")
        return default
    return parsed


def _normalize_priority_block(block: Dict[str, Any], errors: List[str], prefix: str) -> Dict[str, Any]:
    return {
        "high": _ensure_list_of_str(block.get("high", []), f"{prefix}.high", errors),
        "medium": _ensure_list_of_str(block.get("medium", []), f"{prefix}.medium", errors),
        "low": _ensure_list_of_str(block.get("low", []), f"{prefix}.low", errors),
        "equal_groups": _ensure_equal_groups(block.get("equal_groups", []), f"{prefix}.equal_groups", errors),
    }


def _ensure_consonant_tiers(value: Any, name: str, errors: List[str]) -> List[List[str]]:
    if isinstance(value, dict):
        ordered: List[Tuple[int, str, List[str]]] = []
        for raw_key, raw_items in value.items():
            key = str(raw_key)
            items = _ensure_list_of_str(raw_items, f"{name}.{key}", errors)
            if not items:
                continue
            try:
                order = int(key)
            except (ValueError, TypeError):
                order = 10**9
            ordered.append((order, key, items))
        ordered.sort(key=lambda x: (x[0], x[1]))
        return [items for _, _, items in ordered]

    if isinstance(value, list):
        if not value:
            return []
        if all(isinstance(item, str) for item in value):
            return [_ensure_list_of_str(value, name, errors)]

        tiers: List[List[str]] = []
        for idx, item in enumerate(value):
            if isinstance(item, list):
                cleaned = _ensure_list_of_str(item, f"{name}[{idx}]", errors)
                if cleaned:
                    tiers.append(cleaned)
            else:
                errors.append(f"{name}[{idx}] 必须是字符串数组，已忽略")
        return tiers

    errors.append(f"{name} 必须是对象或数组，已忽略")
    return []


def _build_weight_map_from_tiers(
    tiers: List[List[str]],
    level_weights: Dict[str, float],
    fallback_weight: float,
) -> Dict[str, float]:
    if not tiers:
        return {}

    high_weight = float(level_weights.get("high", fallback_weight))
    low_weight = float(level_weights.get("low", fallback_weight))
    steps = max(1, len(tiers) - 1)

    mapping: Dict[str, float] = {}
    for idx, tier in enumerate(tiers):
        if len(tiers) == 1:
            weight = high_weight
        else:
            weight = high_weight - (high_weight - low_weight) * (idx / steps)

        for substat_name in tier:
            key = str(substat_name)
            previous = mapping.get(key)
            if previous is None or weight > previous:
                mapping[key] = float(weight)
    return mapping


def _build_structured_weight_map(
    block: Dict[str, Any],
    level_weights: Dict[str, float],
    fallback_weight: float,
    errors: List[str],
    prefix: str,
) -> Dict[str, float]:
    # 兼容历史配置：high/medium/low/equal_groups
    if any(key in block for key in ("high", "medium", "low", "equal_groups")):
        normalized = _normalize_priority_block(block, errors, prefix)
        return _build_weight_map(normalized, level_weights, fallback_weight)

    # 新配置：consonant 分级（"1" > "2" > ...）
    if "consonant" in block:
        tiers = _ensure_consonant_tiers(block.get("consonant"), f"{prefix}.consonant", errors)
        return _build_weight_map_from_tiers(tiers, level_weights, fallback_weight)

    return {}


def _ensure_cost_main_stats(value: Any, name: str, errors: List[str]) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}

    out: Dict[str, List[str]] = {}
    for raw_key, raw_items in value.items():
        key = str(raw_key or "").strip().upper().replace(" ", "")
        if key not in ("COST4", "COST3", "COST1"):
            continue

        items: List[str] = []
        if isinstance(raw_items, str):
            item = raw_items.strip()
            if item:
                items = [item]
        elif isinstance(raw_items, list):
            items = _ensure_list_of_str(raw_items, f"{name}.{key}", errors)
        elif raw_items is not None:
            errors.append(f"{name}.{key} 必须是字符串或字符串数组，已忽略")

        cleaned = [str(x).strip() for x in items if str(x).strip()]
        if cleaned:
            out[key] = cleaned

    ordered: Dict[str, List[str]] = {}
    for key in ("COST4", "COST3", "COST1"):
        if key in out:
            ordered[key] = out[key]
    return ordered


def _extract_perfect_meta(block: Dict[str, Any], errors: List[str], prefix: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}

    cost_main_stats = _ensure_cost_main_stats(block.get("cost_main_stats"), f"{prefix}.cost_main_stats", errors)
    if cost_main_stats:
        out["cost_main_stats"] = cost_main_stats

    if "consonant" in block:
        tiers = _ensure_consonant_tiers(block.get("consonant"), f"{prefix}.consonant", errors)
        if tiers:
            out["consonant"] = {str(idx + 1): tier for idx, tier in enumerate(tiers)}

    return out


def load_strategy_priority_profile_with_meta(config_path: str | Path) -> StrategyConfigLoadResult:
    errors: List[str] = []
    path = Path(writable_data_path(config_path))

    payload: Dict[str, Any]
    used_default = False
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
            else:
                errors.append("配置文件根节点必须是对象，已回退默认")
                payload = DEFAULT_CONFIG
                used_default = True
        except Exception as e:
            errors.append(f"配置文件解析失败: {e}，已回退默认")
            payload = DEFAULT_CONFIG
            used_default = True
    else:
        errors.append("配置文件不存在，已使用默认配置")
        payload = DEFAULT_CONFIG
        used_default = True

    weights_payload = _ensure_dict(payload.get("weights", {}), "weights", errors)
    level_weights = {
        "high": _safe_float(weights_payload.get("high", 1.0), 1.0, "weights.high", errors),
        "medium": _safe_float(weights_payload.get("medium", 0.7), 0.7, "weights.medium", errors),
        "low": _safe_float(weights_payload.get("low", 0.4), 0.4, "weights.low", errors),
    }
    fallback_weight = _safe_float(weights_payload.get("fallback", 0.5), 0.5, "weights.fallback", errors)

    default_priorities_raw = _ensure_dict(payload.get("default_priorities", {}), "default_priorities", errors)
    default_priorities = _normalize_priority_block(default_priorities_raw, errors, "default_priorities")
    default_weights = _build_weight_map(default_priorities, level_weights, fallback_weight)

    set_weights: Dict[str, Dict[str, float]] = {}
    set_character_weights: Dict[str, Dict[str, Dict[str, float]]] = {}
    set_default_character: Dict[str, str] = {}
    set_meta: Dict[str, Dict[str, Any]] = {}
    set_character_meta: Dict[str, Dict[str, Dict[str, Any]]] = {}
    set_priorities_raw = _ensure_dict(payload.get("set_priorities", {}), "set_priorities", errors)
    for set_name, priorities in set_priorities_raw.items():
        if not isinstance(set_name, str):
            errors.append("set_priorities 中存在非字符串套装名，已忽略")
            continue
        if not isinstance(priorities, dict):
            errors.append(f"set_priorities.{set_name} 必须是对象，已忽略")
            continue

        set_mapping = dict(default_weights)
        meta_set = _extract_perfect_meta(priorities, errors, f"set_priorities.{set_name}")
        if meta_set:
            set_meta[set_name] = meta_set

        set_override = _build_structured_weight_map(
            priorities,
            level_weights=level_weights,
            fallback_weight=fallback_weight,
            errors=errors,
            prefix=f"set_priorities.{set_name}",
        )
        if set_override:
            set_mapping.update(set_override)
            set_weights[set_name] = dict(set_mapping)

        by_character_raw = priorities.get("by_character", {})
        if isinstance(by_character_raw, dict) and by_character_raw:
            role_mapping: Dict[str, Dict[str, float]] = {}
            role_meta_mapping: Dict[str, Dict[str, Any]] = {}
            for role_name, role_priorities in by_character_raw.items():
                if not isinstance(role_name, str):
                    errors.append(f"set_priorities.{set_name}.by_character 中存在非字符串角色名，已忽略")
                    continue
                if not isinstance(role_priorities, dict):
                    errors.append(f"set_priorities.{set_name}.by_character.{role_name} 必须是对象，已忽略")
                    continue

                role_meta = _extract_perfect_meta(
                    role_priorities,
                    errors,
                    f"set_priorities.{set_name}.by_character.{role_name}",
                )
                if role_meta:
                    role_meta_mapping[role_name] = role_meta

                role_override = _build_structured_weight_map(
                    role_priorities,
                    level_weights=level_weights,
                    fallback_weight=fallback_weight,
                    errors=errors,
                    prefix=f"set_priorities.{set_name}.by_character.{role_name}",
                )
                if not role_override:
                    continue

                merged_role = dict(set_mapping)
                merged_role.update(role_override)
                role_mapping[role_name] = merged_role

            if role_meta_mapping:
                set_character_meta[set_name] = role_meta_mapping

            if role_mapping:
                set_character_weights[set_name] = role_mapping
                preferred_role = priorities.get("selected_character")
                if isinstance(preferred_role, str) and preferred_role in role_mapping:
                    set_default_character[set_name] = preferred_role
                else:
                    set_default_character[set_name] = next(iter(role_mapping.keys()))

                # 即使套装层没有单独覆盖，也保留一份 set 映射作为角色缺省兜底。
                if set_name not in set_weights:
                    set_weights[set_name] = dict(set_mapping)

    profile = StrategyPriorityProfile(
        fallback_weight=fallback_weight,
        weights_by_level=level_weights,
        default_weights=default_weights,
        set_weights=set_weights,
        set_character_weights=set_character_weights,
        set_default_character=set_default_character,
        set_meta=set_meta,
        set_character_meta=set_character_meta,
    )
    return StrategyConfigLoadResult(profile=profile, used_default=used_default, errors=errors)


def load_strategy_priority_profile(config_path: str | Path) -> StrategyPriorityProfile:
    return load_strategy_priority_profile_with_meta(config_path).profile
