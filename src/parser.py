"""
parser.py — 将 OCR 原始文本正规化为结构化声骸词缀。
支持模糊纠错与百分比/固定值提取。
"""
import re
from typing import Dict, Optional, List

# ── 已知词缀名称 → 标准化名（持续扩充）──────────────────────
KNOWN_AFFIXES = {
    "攻击": "攻击",
    "生命": "生命",
    "防御": "防御",
    "暴击": "暴击",
    "暴击伤害": "暴击伤害",
    "暴伤": "暴击伤害",
    "共鸣效率": "共鸣效率",
    "共呜效率": "共鸣效率",
    "能量回复": "共鸣效率",
    # 元素伤害加成
    "湮灭伤害加成": "湮灭伤害加成",
    "浸灭伤害加成": "湮灭伤害加成",
    "热熔伤害加成": "热熔伤害加成",
    "导电伤害加成": "导电伤害加成",
    "号电伤害加成": "导电伤害加成",
    "尊电伤害加成": "导电伤害加成",
    "气动伤害加成": "气动伤害加成",
    "气功伤害加成": "气动伤害加成",
    "衍射伤害加成": "衍射伤害加成",
    "行射伤害加成": "衍射伤害加成",
    "冷凝伤害加成": "冷凝伤害加成",
    "冷的伤害加成": "冷凝伤害加成",
    "湮灭伤害": "湮灭伤害加成",
    "热熔伤害": "热熔伤害加成",
    "导电伤害": "导电伤害加成",
    "气动伤害": "气动伤害加成",
    "衍射伤害": "衍射伤害加成",
    "冷凝伤害": "冷凝伤害加成",
    # 通用伤害加成
    "属性伤害加成": "属性伤害加成",
    "普攻伤害加成": "普攻伤害加成",
    "重击伤害加成": "重击伤害加成",
    "共鸣技能伤害加成": "共鸣技能伤害加成",
    "共鸣解放伤害加成": "共鸣解放伤害加成",
    "治疗效果加成": "治疗效果加成",
}

# ── 常见 OCR 纠错映射（字符级别）──
_CHAR_FIXES = {
    "O": "0", "o": "0", "I": "1", "l": "1",
    "，": ",", "。": ".", "％": "%",
}

_pct_re = re.compile(r"([\d]+(?:\.[\d]+)?)\s*[%％]")
_flat_re = re.compile(r"[+＋]\s*([\d]+(?:\.[\d]+)?)")
_num_re = re.compile(r"([\d]+(?:\.[\d]+)?)")


def _fix_chars(text: str) -> str:
    for old, new in _CHAR_FIXES.items():
        text = text.replace(old, new)
    return text


def normalize_affix_name(raw: str) -> str:
    """模糊匹配已知词缀名称并标准化。"""
    raw = raw.strip().replace(" ", "")
    # 按键名长度降序排序，防止短词（如"暴击"）抢先匹配包含长词（如"暴击伤害"）的文本
    sorted_keys = sorted(KNOWN_AFFIXES.keys(), key=len, reverse=True)
    for key in sorted_keys:
        if key in raw:
            return KNOWN_AFFIXES[key]
    return raw


def parse_line(text: str) -> Optional[Dict]:
    """
    解析单行词条文字。
    返回 {"name": str, "value": float, "is_pct": bool} 或 None。
    示例: "攻击+12.3%"  →  {"name":"攻击", "value":12.3, "is_pct":True}
    """
    text = _fix_chars(text)
    name = normalize_affix_name(text)

    m_pct = _pct_re.search(text)
    if m_pct:
        return {"name": name, "value": float(m_pct.group(1)), "is_pct": True}

    m_flat = _flat_re.search(text)
    if m_flat:
        return {"name": name, "value": float(m_flat.group(1)), "is_pct": False}

    # 尝试任意数字
    m_num = _num_re.search(text)
    if m_num and name != text.strip():
        return {"name": name, "value": float(m_num.group(1)), "is_pct": False}

    return None


def parse_texts(texts: List[str]) -> List[Dict]:
    """批量解析 OCR 输出行，过滤无效行。"""
    out = []
    for t in texts:
        r = parse_line(t)
        if r:
            out.append(r)
    return out


# ── 自测 ──
if __name__ == "__main__":
    samples = [
        "攻击+12.3%",
        "生命+4200",
        "暴击 5.8%",
        "防御 +100",
        "暴伤+22.O%",  # OCR 错误：O→0
        "共鸣效率+8.5%",
    ]
    for s in samples:
        print(f"{s!r:30s} → {parse_line(s)}")
