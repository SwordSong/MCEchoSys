"""probability.py — 强化概率推断引擎。
阶段 1: FrequencyModel  — 纯频率统计
阶段 2: BayesianUpdater — 在线贝叶斯更新先验/后验
阶段 3: SubstatPosteriorModel — 基于历史开孔记录的分层贝叶斯辅音预测
可扩展: IProbEngine 接口 → 马尔可夫 / 条件概率矩阵
"""
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Any, Iterable, Optional


class IProbEngine(ABC):
    """概率引擎公共接口。"""

    @abstractmethod
    def update(self, observation: str) -> None: ...

    @abstractmethod
    def predict(self) -> Dict[str, float]: ...

    @abstractmethod
    def reset(self) -> None: ...


class FrequencyModel(IProbEngine):
    """简单频率统计模型。"""

    def __init__(self):
        self.counts: Counter = Counter()
        self.total: int = 0

    def update(self, item: str):
        self.counts[item] += 1
        self.total += 1

    def predict(self) -> Dict[str, float]:
        if self.total == 0:
            return {}
        return {k: v / self.total for k, v in self.counts.items()}

    def reset(self):
        self.counts.clear()
        self.total = 0


class BayesianUpdater(IProbEngine):
    """Dirichlet-Multinomial 在线贝叶斯更新。

    prior: {label: alpha} — Dirichlet 超参数，默认 alpha=1（均匀先验）。
    """

    def __init__(self, prior: Dict[str, float] | None = None):
        self.prior = prior or {}
        self.counts: Counter = Counter()

    def update(self, observation: str):
        self.counts[observation] += 1
        # 自动扩充先验
        if observation not in self.prior:
            self.prior[observation] = 1.0

    def predict(self) -> Dict[str, float]:
        return self.posterior()

    def posterior(self) -> Dict[str, float]:
        all_keys = set(self.counts) | set(self.prior)
        alpha_sum = sum(self.prior.get(k, 1.0) for k in all_keys)
        n = sum(self.counts.values())
        denom = n + alpha_sum
        if denom == 0:
            return {}
        return {
            k: (self.counts.get(k, 0) + self.prior.get(k, 1.0)) / denom
            for k in all_keys
        }

    def reset(self):
        self.counts.clear()


class SubstatPosteriorModel:
    """用本地历史记录预测当前声骸剩余孔位最可能出的辅音。

    模型思路：
    - 每一层都是 Dirichlet-Multinomial 后验，避免小样本出现 0% 概率。
    - 分层融合：全局 / 同 COST / 同 COST+主词条 / 同 COST+套装+主词条。
    - 当前已出现的辅音和主词条会从候选池中移除。
    - 攻击、生命、防御区分固定值和百分比；例如 `攻击%` 与固定 `攻击` 是两个候选。
    """

    BASIC_STATS = {"攻击", "生命", "防御"}

    def __init__(
        self,
        prior_alpha: float = 1.0,
        min_layer_samples: int = 8,
        layer_weights: Optional[Dict[str, float]] = None,
    ):
        self.prior_alpha = max(float(prior_alpha), 0.001)
        self.min_layer_samples = max(int(min_layer_samples), 1)
        self.layer_weights = layer_weights or {
            "exact": 0.45,
            "cost_main": 0.25,
            "cost": 0.20,
            "global": 0.10,
        }
        self.records: List[Dict[str, Any]] = []

    @classmethod
    def _infer_is_pct(cls, name: str, value: Any = None, is_pct: Any = None) -> Optional[bool]:
        if is_pct is not None:
            return bool(is_pct)
        if name not in cls.BASIC_STATS:
            return None
        if isinstance(value, (int, float)):
            # 当前配置中基础三维百分比值都小于 20，固定值都明显大于 20。
            return float(value) < 20
        return None

    @classmethod
    def substat_key(cls, name: Any, value: Any = None, is_pct: Any = None) -> str:
        stat_name = str(name or "").strip()
        if not stat_name:
            return ""
        if stat_name.endswith("%"):
            return stat_name
        inferred_pct = cls._infer_is_pct(stat_name, value=value, is_pct=is_pct)
        if stat_name in cls.BASIC_STATS and inferred_pct is True:
            return f"{stat_name}%"
        return stat_name

    @classmethod
    def _candidate_keys(cls, candidate_pool: Optional[Iterable[Any]]) -> List[str]:
        keys = []
        for raw in candidate_pool or []:
            if isinstance(raw, dict):
                name = raw.get("name") or raw.get("stat_name")
                is_pct = raw.get("is_pct", raw.get("is_percent"))
                key = cls.substat_key(name, is_pct=is_pct)
            else:
                key = cls.substat_key(raw)
            if key and key not in keys:
                keys.append(key)
        return keys

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _clean_cost(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def fit(self, records: Iterable[Dict[str, Any]]) -> "SubstatPosteriorModel":
        self.records = []
        for record in records:
            name = self._clean_text(record.get("substat_name"))
            key = self.substat_key(
                name,
                value=record.get("substat_value"),
                is_pct=record.get("is_pct", record.get("substat_is_pct")),
            )
            if not key:
                continue
            self.records.append(
                {
                    "key": key,
                    "cost": self._clean_cost(record.get("cost")),
                    "set_name": self._clean_text(record.get("set_name")),
                    "main_stat": self._clean_text(record.get("main_stat")),
                    "slot_index": self._clean_cost(record.get("slot_index")),
                }
            )
        return self

    def _blocked_keys(self, context: Dict[str, Any]) -> set[str]:
        blocked = set()
        main_stat = self._clean_text(context.get("main_stat"))
        if main_stat:
            main_is_pct = main_stat.endswith("%")
            main_name = main_stat[:-1] if main_is_pct else main_stat
            key = self.substat_key(main_name, is_pct=True if main_is_pct else None)
            if key:
                blocked.add(key)

        for sub in context.get("existing_substats") or []:
            if isinstance(sub, dict):
                key = self.substat_key(
                    sub.get("name"),
                    value=sub.get("value"),
                    is_pct=sub.get("is_pct"),
                )
            else:
                key = self.substat_key(sub)
            if key:
                blocked.add(key)
        return blocked

    def _records_for_layer(self, layer: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        cost = self._clean_cost(context.get("cost"))
        set_name = self._clean_text(context.get("set_name"))
        main_stat = self._clean_text(context.get("main_stat"))

        if layer == "global":
            return list(self.records)
        if layer == "cost":
            if cost is None:
                return []
            return [r for r in self.records if r.get("cost") == cost]
        if layer == "cost_main":
            if cost is None or not main_stat:
                return []
            return [
                r for r in self.records
                if r.get("cost") == cost and r.get("main_stat") == main_stat
            ]
        if layer == "exact":
            if cost is None or not set_name or not main_stat:
                return []
            return [
                r for r in self.records
                if r.get("cost") == cost
                and r.get("set_name") == set_name
                and r.get("main_stat") == main_stat
            ]
        return []

    def _posterior_for_records(self, records: List[Dict[str, Any]], candidates: List[str]) -> Dict[str, float]:
        counts = Counter(r["key"] for r in records if r.get("key") in candidates)
        sample_count = sum(counts.values())
        denom = sample_count + self.prior_alpha * len(candidates)
        if denom <= 0:
            return {}
        return {
            key: (counts.get(key, 0) + self.prior_alpha) / denom
            for key in candidates
        }

    def predict(
        self,
        context: Dict[str, Any],
        candidate_pool: Optional[Iterable[Any]] = None,
        top_n: int = 8,
    ) -> Dict[str, Any]:
        candidates = self._candidate_keys(candidate_pool)
        if not candidates:
            candidates = sorted({r["key"] for r in self.records if r.get("key")})

        blocked = self._blocked_keys(context)
        candidates = [key for key in candidates if key not in blocked]
        if not candidates:
            return {
                "probabilities": {},
                "predictions": [],
                "samples": {},
                "blocked": sorted(blocked),
            }

        layer_probs: Dict[str, Dict[str, float]] = {}
        layer_samples: Dict[str, int] = {}
        effective_weights: Dict[str, float] = {}

        for layer, base_weight in self.layer_weights.items():
            layer_records = self._records_for_layer(layer, context)
            sample_count = len(layer_records)
            layer_samples[layer] = sample_count
            layer_probs[layer] = self._posterior_for_records(layer_records, candidates)
            confidence = min(1.0, sample_count / self.min_layer_samples)
            effective_weights[layer] = max(float(base_weight), 0.0) * confidence

        if sum(effective_weights.values()) <= 0:
            effective_weights["global"] = 1.0
            layer_probs["global"] = self._posterior_for_records([], candidates)
            layer_samples["global"] = 0

        weight_sum = sum(effective_weights.values())
        probabilities = {key: 0.0 for key in candidates}
        for layer, weight in effective_weights.items():
            if weight <= 0:
                continue
            normalized_weight = weight / weight_sum
            for key, prob in layer_probs.get(layer, {}).items():
                probabilities[key] += normalized_weight * prob

        predictions = [
            {
                "key": key,
                "name": key,
                "probability": probabilities[key],
            }
            for key in sorted(probabilities, key=probabilities.get, reverse=True)[:max(1, int(top_n))]
        ]

        return {
            "probabilities": probabilities,
            "predictions": predictions,
            "samples": layer_samples,
            "blocked": sorted(blocked),
        }


@dataclass
class ActionStrategyResult:
    recommended_action: str
    reason: str
    single_score: float
    multi_score: float
    single_samples: int
    multi_samples: int


class ActionStrategyAdvisor:
    """基于历史动作数据，给出单开/多开建议。

    输入样本格式：
    {
        "action_type": "single" | "multi",
        "value_tier": int | None,
        "is_historical_unknown": bool,
        "cost": int | None,
    }
    """

    def __init__(self, min_samples_for_confident: int = 10):
        self.min_samples_for_confident = min_samples_for_confident

    @staticmethod
    def _normalize_tier(value_tier: Any) -> float:
        if value_tier is None:
            return 0.5
        try:
            tier = float(value_tier)
        except (ValueError, TypeError):
            return 0.5
        if tier <= 0:
            return 0.5
        if tier > 4:
            tier = 4
        return tier / 4.0

    @staticmethod
    def _priority_weight(record: Dict[str, Any], priority_weights: Dict[str, float] | None, fallback: float = 0.5) -> float:
        if not priority_weights:
            return fallback
        name = str(record.get("substat_name", ""))
        if name in {"攻击", "防御"} and name in priority_weights:
            is_pct = record.get("is_pct", record.get("substat_is_pct"))
            if is_pct is None and isinstance(record.get("substat_value"), (int, float)):
                is_pct = float(record["substat_value"]) < 20
            if is_pct is False:
                return fallback
        return float(priority_weights.get(name, fallback))

    @classmethod
    def _matches_priority_name(cls, record: Dict[str, Any], target_name: str) -> bool:
        name = str(record.get("name", record.get("substat_name", ""))).strip()
        target = str(target_name or "").strip()
        if not name or name != target:
            return False
        if name in {"攻击", "防御"}:
            is_pct = record.get("is_pct", record.get("substat_is_pct"))
            if is_pct is None and isinstance(record.get("value", record.get("substat_value")), (int, float)):
                is_pct = float(record.get("value", record.get("substat_value"))) < 20
            return is_pct is True
        return True

    @staticmethod
    def _tier_sort_key(raw_key: Any) -> tuple[int, Any]:
        text = str(raw_key)
        try:
            return (0, int(text))
        except (TypeError, ValueError):
            order = {"high": 1, "medium": 2, "low": 3}
            return (1, order.get(text, 99), text)

    @staticmethod
    def _slot_cost_weight(slot_index: int) -> float:
        # 相对成本：越靠后的孔位强化材料越贵。这里只做策略权重，不绑定具体材料数量。
        weights = {
            1: 1.0,
            2: 1.7,
            3: 2.8,
            4: 4.6,
            5: 7.5,
        }
        return float(weights.get(max(1, min(5, int(slot_index))), 7.5))

    @classmethod
    def _prediction_matches_target(cls, key: Any, target_name: str) -> bool:
        pred = str(key or "").strip()
        target = str(target_name or "").strip()
        if not pred or not target:
            return False
        if target.endswith("%"):
            return pred == target
        if target in {"攻击", "防御"}:
            return pred == f"{target}%"
        if target == "生命":
            return pred in {"生命", "生命%"}
        return pred == target

    @classmethod
    def _next_hit_probability(cls, substat_posterior: Dict[str, Any] | None, targets: List[str]) -> float:
        if not isinstance(substat_posterior, dict) or not targets:
            return 0.0
        probabilities = substat_posterior.get("probabilities") or {}
        if not isinstance(probabilities, dict):
            return 0.0
        total = 0.0
        for key, raw_prob in probabilities.items():
            if not any(cls._prediction_matches_target(key, target) for target in targets):
                continue
            try:
                total += float(raw_prob)
            except (TypeError, ValueError):
                continue
        return round(max(0.0, min(1.0, total)), 4)

    @classmethod
    def _target_groups(
        cls,
        perfect_consonant: Dict[str, Any] | None,
        priority_weights: Dict[str, float] | None,
        fallback: float,
    ) -> tuple[List[str], List[str]]:
        all_targets: List[str] = []
        top_targets: List[str] = []

        if isinstance(perfect_consonant, dict) and perfect_consonant:
            for tier_key in sorted(perfect_consonant.keys(), key=cls._tier_sort_key):
                values = perfect_consonant.get(tier_key) or []
                if isinstance(values, str):
                    values = [values]
                tier_targets = [str(v).strip() for v in values if str(v).strip()]
                for name in tier_targets:
                    if name not in all_targets:
                        all_targets.append(name)
                sort_key = cls._tier_sort_key(tier_key)
                is_top_tier = (sort_key[0] == 0 and sort_key[1] <= 2) or str(tier_key) in {"high", "medium"}
                if is_top_tier:
                    for name in tier_targets:
                        if name not in top_targets:
                            top_targets.append(name)

        if not all_targets and priority_weights:
            weighted = [
                (name, float(weight))
                for name, weight in priority_weights.items()
                if str(name).strip() and float(weight) > float(fallback)
            ]
            weighted.sort(key=lambda item: item[1], reverse=True)
            all_targets = [name for name, _ in weighted]
            top_cutoff = weighted[1][1] if len(weighted) > 1 else (weighted[0][1] if weighted else 0.0)
            top_targets = [name for name, weight in weighted if weight >= top_cutoff]

        return all_targets, top_targets or all_targets[:2]

    @classmethod
    def analyze_cost(
        cls,
        substats: List[Dict[str, Any]],
        perfect_consonant: Dict[str, Any] | None = None,
        priority_weights: Dict[str, float] | None = None,
        substat_posterior: Dict[str, Any] | None = None,
        fallback: float = 0.5,
        max_slots: int = 5,
    ) -> Dict[str, Any]:
        visible_substats = [
            s for s in (substats or [])
            if str(s.get("name", s.get("substat_name", ""))).strip()
        ][:max_slots]
        opened_count = len(visible_substats)
        remaining_slots = max(0, max_slots - opened_count)
        all_targets, top_targets = cls._target_groups(perfect_consonant, priority_weights, fallback)

        effective_hit_indexes = []
        top_hit_indexes = []
        weighted_total = 0.0
        for idx, sub in enumerate(visible_substats, start=1):
            if any(cls._matches_priority_name(sub, target) for target in all_targets):
                effective_hit_indexes.append(idx)
            if any(cls._matches_priority_name(sub, target) for target in top_targets):
                top_hit_indexes.append(idx)
            weighted_total += cls._priority_weight(
                {
                    "substat_name": sub.get("name", sub.get("substat_name", "")),
                    "substat_value": sub.get("value", sub.get("substat_value")),
                    "is_pct": sub.get("is_pct", sub.get("substat_is_pct")),
                },
                priority_weights,
                fallback=fallback,
            )

        first_three_top_hits = [idx for idx in top_hit_indexes if idx <= 3]
        first_two_effective_hits = [idx for idx in effective_hit_indexes if idx <= 2]
        effective_hit_count = len(effective_hit_indexes)
        top_hit_count = len(top_hit_indexes)
        utility_rate = round(weighted_total / opened_count, 4) if opened_count else 0.0
        next_slot_index = min(max_slots, opened_count + 1)
        next_slot_cost = 0.0 if remaining_slots <= 0 else cls._slot_cost_weight(next_slot_index)
        remaining_cost = round(sum(cls._slot_cost_weight(i) for i in range(opened_count + 1, max_slots + 1)), 2)
        max_next_cost = cls._slot_cost_weight(max_slots)
        cost_pressure = round(next_slot_cost / max_next_cost, 4) if max_next_cost > 0 else 0.0
        next_effective_probability = cls._next_hit_probability(substat_posterior, all_targets)
        next_top_probability = cls._next_hit_probability(substat_posterior, top_targets)
        has_prediction = bool(isinstance(substat_posterior, dict) and substat_posterior.get("probabilities"))

        recommended_action = ""
        reason = ""
        suggestions: List[str] = []

        if opened_count >= max_slots:
            recommended_action = "finished"
            reason = "已经开满5个辅音，无剩余孔位可预测，转为最终词条评估"
            suggestions = ["停止继续开孔", "按当前有效词条数量判断是否保留"]
        elif opened_count >= 2 and len(first_two_effective_hits) == 0:
            recommended_action = "switch_echo"
            reason = "前2个辅音未命中有效词条，继续强化的沉没成本不值得追加"
            suggestions = ["直接放弃当前声骸", "换下一只声骸", "保留材料给更好的胚子"]
        elif opened_count >= 3 and len(top_targets) >= 2 and len(first_three_top_hits) == 0:
            recommended_action = "switch_echo"
            reason = "前3个辅音未命中1/2档核心词条，后续补救成本高"
            suggestions = ["停止强化当前声骸", "换下一只声骸", "如果当前客户端连续低效，可重启客户端后再继续"]
        elif opened_count >= 3 and len(all_targets) >= 4 and effective_hit_count <= 1:
            recommended_action = "switch_echo"
            reason = "已开3个辅音但有效命中不足，继续投入的有用率偏低"
            suggestions = ["及时抽离", "换声骸继续筛选"]
        elif opened_count >= 4 and effective_hit_count <= 2:
            recommended_action = "switch_echo"
            reason = "已开4个辅音仅命中2个以内有效词条，继续补第5孔成本过高"
            suggestions = ["放弃当前声骸", "保留强化材料给新胚子"]
        elif opened_count >= 4 and len(top_targets) >= 2 and top_hit_count < 2:
            recommended_action = "switch_echo"
            reason = "已投入4个辅音仍未形成足够核心命中，建议停止追加成本"
            suggestions = ["停止强化当前声骸", "保留资源给更好的胚子"]
        elif has_prediction and opened_count >= 2 and top_hit_count >= 2 and next_effective_probability < 0.35:
            recommended_action = "park_echo"
            reason = "当前前缀很好，但下一孔有效概率偏低且继续强化成本会上升"
            suggestions = ["暂存当前声骸", "先换另一个声骸强化", "当下一孔有效概率回升再回到当前声骸"]
        elif has_prediction and opened_count >= 2 and top_hit_count >= 2 and next_effective_probability >= 0.55:
            recommended_action = "continue_echo"
            reason = "当前核心词条优秀，且下一孔有效概率足够高，可以回到这只声骸继续强化"
            suggestions = ["继续强化当前声骸", "优先观察下一孔结果"]
        elif has_prediction and opened_count >= 3 and cost_pressure >= 0.55 and next_effective_probability < 0.40:
            recommended_action = "switch_echo"
            reason = "下一孔成本较高，但预测有效概率偏低"
            suggestions = ["停止追加成本", "换另一个声骸强化"]

        summary = (
            f"成本分析: 已开{opened_count}/{max_slots}孔，"
            f"有效{effective_hit_count}/{len(all_targets)}，"
            f"核心{top_hit_count}/{len(top_targets)}，"
            f"效率{utility_rate * 100:.0f}%，"
            f"下孔成本{next_slot_cost:.1f}，"
            f"下孔有效率{next_effective_probability * 100:.0f}%"
        )

        return {
            "opened_count": opened_count,
            "remaining_slots": remaining_slots,
            "effective_target_count": len(all_targets),
            "top_target_count": len(top_targets),
            "effective_hit_count": effective_hit_count,
            "top_hit_count": top_hit_count,
            "first_two_effective_hit_count": len(first_two_effective_hits),
            "first_three_top_hit_count": len(first_three_top_hits),
            "utility_rate": utility_rate,
            "next_slot_index": next_slot_index if remaining_slots > 0 else None,
            "next_slot_cost": next_slot_cost,
            "remaining_cost": remaining_cost,
            "cost_pressure": cost_pressure,
            "next_effective_probability": next_effective_probability,
            "next_top_probability": next_top_probability,
            "recommended_action": recommended_action,
            "stop_recommended": recommended_action in {"switch_echo", "park_echo", "finished"},
            "reason": reason,
            "summary": summary,
            "suggestions": suggestions,
            "effective_targets": all_targets,
            "top_targets": top_targets,
        }

    def _score(
        self,
        records: List[Dict[str, Any]],
        action_type: str,
        priority_weights: Dict[str, float] | None = None,
    ) -> tuple[float, int]:
        subset = [r for r in records if r.get("action_type") == action_type and not r.get("is_historical_unknown", False)]
        if not subset:
            return 0.0, 0

        weighted_tiers = []
        high_roll_weight = 0.0
        for r in subset:
            w = self._priority_weight(r, priority_weights)
            t = self._normalize_tier(r.get("value_tier"))
            weighted_tiers.append(t * w)
            if (r.get("value_tier") or 0) >= 3:
                high_roll_weight += w

        avg_tier = sum(weighted_tiers) / len(subset)
        high_roll_rate = high_roll_weight / len(subset)
        confidence = min(1.0, len(subset) / max(1, self.min_samples_for_confident))

        score = (avg_tier * 0.7 + high_roll_rate * 0.3) * (0.6 + 0.4 * confidence)
        return round(score, 4), len(subset)

    def recommend(
        self,
        records: List[Dict[str, Any]],
        tunable_slots_count: int,
        priority_weights: Dict[str, float] | None = None,
    ) -> ActionStrategyResult:
        single_score, single_samples = self._score(records, "single", priority_weights=priority_weights)
        multi_score, multi_samples = self._score(records, "multi", priority_weights=priority_weights)

        if single_samples == 0 and multi_samples == 0:
            if tunable_slots_count >= 2:
                return ActionStrategyResult(
                    recommended_action="multi",
                    reason="暂无历史样本，当前存在多个可调谐孔位，优先多开提高效率",
                    single_score=0.0,
                    multi_score=0.0,
                    single_samples=0,
                    multi_samples=0,
                )
            return ActionStrategyResult(
                recommended_action="single",
                reason="暂无历史样本，当前可调谐孔位有限，优先单开便于观察结果",
                single_score=0.0,
                multi_score=0.0,
                single_samples=0,
                multi_samples=0,
            )

        if multi_score > single_score:
            reason = f"多开历史得分更高（multi={multi_score:.3f} > single={single_score:.3f}）"
            return ActionStrategyResult("multi", reason, single_score, multi_score, single_samples, multi_samples)

        reason = f"单开历史得分更高（single={single_score:.3f} >= multi={multi_score:.3f}）"
        return ActionStrategyResult("single", reason, single_score, multi_score, single_samples, multi_samples)


# ── 自测 ──
if __name__ == "__main__":
    fm = FrequencyModel()
    for x in ["攻击", "暴击", "攻击", "生命", "攻击"]:
        fm.update(x)
    print("freq:", fm.predict())

    bu = BayesianUpdater()
    for x in ["攻击", "暴击", "攻击"]:
        bu.update(x)
    print("bayes:", bu.posterior())
