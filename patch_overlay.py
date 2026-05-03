import json
import re

with open('src/ui/overlay.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Add _flatten_priority_names method
flatten_method = '''
    def _flatten_priority_names(self, perfect_consonant: list) -> list:
        names = []
        for x in perfect_consonant:
            if isinstance(x, list):
                names.extend(x)
            else:
                names.append(x)
        return names
        
    def _html_line(self, text, color):
        return f'<span style="color:{color}">{text}</span>'
        
    def update_scene_debug(self, scene_match, frame_shape=None, uid_crop_box=None):
'''

text = re.sub(r'\s*def update_scene_debug\(self, scene_match: Optional\[Dict\[str, Any\]\], frame_shape=None, uid_crop_box=None\):', flatten_method, text)

# Now find the place to insert probability text
old_slots_code = '''        slot_lines = []
        if obs and obs.get("slot_states"):
            ui_mode = obs.get("ui_mode", "enhance_panel")
            level = obs.get("level")
            title = "槽位状态(开孔界面):" if ui_mode == "tune_panel" else "槽位状态(强化界面):"
            slot_lines.append(title)
            for slot in obs["slot_states"]:
                slot_lines.append(f"  [{slot.get('slot_index')}] {slot.get('text')}")'''

new_slots_code = '''        slot_lines = []
        if obs and obs.get("slot_states"):
            ui_mode = obs.get("ui_mode", "enhance_panel")
            level = obs.get("level")
            title = "槽位状态(开孔界面):" if ui_mode == "tune_panel" else "槽位状态(强化界面):"
            slot_active_color = "#f0d38f"
            slot_no_effect_color = "#8b9298"
            
            # 辅音组合概率
            perfect_consonant = []
            if strategy_advice and strategy_advice.get("perfect_consonant"):
                perfect_consonant = strategy_advice.get("perfect_consonant")
            priority_names = self._flatten_priority_names(perfect_consonant)
            
            slots = obs.get("slot_states")
            # 计算辅音出现纯数学概率
            double_stat_names = {"攻击", "生命", "防御"}
            initial_valid = 0
            for p_name in priority_names:
                if p_name in double_stat_names:
                    initial_valid += 2
                else:
                    initial_valid += 1
                    
            rolled_names = []
            valid_rolled_count = 0
            for s in slots:
                s_name = str(s.get("name") or "").strip()
                if s_name:
                    rolled_names.append(s_name)
                    if s_name in priority_names:
                        valid_rolled_count += 1
                        
            pool_size = max(1, 13 - len(rolled_names))
            valid_count = max(0, initial_valid - valid_rolled_count)
            unrolled_slots = 5 - len(rolled_names)
            
            prob_text = ""
            if unrolled_slots > 0 and priority_names:
                single_prob = valid_count / pool_size
                import math
                invalid_count = pool_size - valid_count
                prob_all_invalid = (math.comb(invalid_count, unrolled_slots) / math.comb(pool_size, unrolled_slots)) if invalid_count >= unrolled_slots else 0.0
                prob_at_least_one = 1.0 - prob_all_invalid
                
                if unrolled_slots == 1:
                    prob_text = f"  (孔位出货率: {single_prob*100:.1f}%)"
                else:
                    prob_text = f"  (单孔出货率: {single_prob*100:.1f}% | 剩余{unrolled_slots}孔至少出1: {prob_at_least_one*100:.1f}%)"
            
            slot_lines.append(f"{title} 当前辅音状态{prob_text}")
            
            for slot in slots:
                text_content = slot.get('text')
                s_name = str(slot.get("name") or "").strip()
                color = slot_active_color if s_name in priority_names else slot_no_effect_color
                slot_lines.append(self._html_line(f"  [{slot.get('slot_index')}] {text_content}", color))'''

if old_slots_code in text:
    text = text.replace(old_slots_code, new_slots_code)
else:
    print("WARNING: Could not find old slots code")

with open('src/ui/overlay.py', 'w', encoding='utf-8') as f:
    f.write(text)
print("Updated overlay.py")
