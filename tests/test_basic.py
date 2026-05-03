"""tests/test_basic.py — 单元测试：parser、probability、preprocess。"""
import datetime
import json
import numpy as np


# ── parser ──
def test_parse_line_pct():
    from src.parser import parse_line
    r = parse_line("攻击+12.3%")
    assert r is not None
    assert r["name"] == "攻击"
    assert abs(r["value"] - 12.3) < 0.01
    assert r["is_pct"] is True


def test_parse_line_flat():
    from src.parser import parse_line
    r = parse_line("生命+4200")
    assert r is not None
    assert r["name"] == "生命"
    assert r["value"] == 4200
    assert r["is_pct"] is False


def test_parse_line_ocr_fix():
    from src.parser import parse_line
    r = parse_line("暴伤+22.O%")  # O → 0
    assert r is not None
    assert abs(r["value"] - 22.0) < 0.01


def test_parse_texts_batch():
    from src.parser import parse_texts
    results = parse_texts(["攻击+10%", "无效文字", "防御+50"])
    assert len(results) == 2


def test_normalize_affix():
    from src.parser import normalize_affix_name
    assert normalize_affix_name("攻击") == "攻击"
    assert normalize_affix_name("暴伤") == "暴击伤害"


# ── probability ──
def test_frequency_model():
    from src.probability import FrequencyModel
    m = FrequencyModel()
    m.update("A")
    m.update("B")
    m.update("A")
    p = m.predict()
    assert abs(p["A"] - 2/3) < 0.01
    assert abs(p["B"] - 1/3) < 0.01


def test_bayesian_updater():
    from src.probability import BayesianUpdater
    b = BayesianUpdater()
    b.update("X")
    b.update("X")
    b.update("Y")
    post = b.posterior()
    assert post["X"] > post["Y"]
    assert abs(sum(post.values()) - 1.0) < 0.01


def test_bayesian_reset():
    from src.probability import BayesianUpdater
    b = BayesianUpdater()
    b.update("A")
    b.reset()
    assert sum(b.counts.values()) == 0


# ── preprocess ──
def test_crop_bbox():
    from src.preprocess import crop_bbox
    img = np.zeros((100, 200, 3), dtype=np.uint8)
    crop = crop_bbox(img, [10, 10, 50, 50])
    assert crop.shape == (40, 40, 3)


def test_enhance_for_ocr():
    from src.preprocess import enhance_for_ocr
    img = np.random.randint(0, 255, (60, 120, 3), dtype=np.uint8)
    out = enhance_for_ocr(img)
    assert len(out.shape) == 2  # 应输出灰度/二值图
    assert out.shape[0] == 60


def test_upscale_if_small():
    from src.preprocess import upscale_if_small
    img = np.zeros((20, 80, 3), dtype=np.uint8)
    out = upscale_if_small(img, min_height=48)
    assert out.shape[0] >= 48


# ── db / restart detection ──
def test_mark_client_started_first_time_not_restart():
    from src.db import init_db, Account, generate_db_write_key, mark_client_started

    write_key = generate_db_write_key()
    Session = init_db("sqlite:///:memory:", write_key=write_key)
    s = Session()
    s.authorize_writes(write_key)
    acc = Account(uid="123456789", account_hash="h1", name="u")
    s.add(acc)
    s.flush()

    dt = datetime.datetime(2026, 3, 1, 12, 0, 0)
    restarted = mark_client_started(s, acc, dt, detected_pid=111)
    s.commit()

    assert restarted is False
    assert acc.account_hash is not None
    assert len(acc.account_hash) == 16
    assert acc.last_client_start_at == dt
    assert acc.last_client_pid == 111
    assert acc.total_enhance == 0
    assert acc.today_enhance == 0
    assert acc.client_enhance == 0
    s.close()


def test_mark_client_started_newer_time_is_restart():
    from src.db import init_db, Account, LoginRecord, generate_db_write_key, local_now, mark_client_started

    write_key = generate_db_write_key()
    Session = init_db("sqlite:///:memory:", write_key=write_key)
    s = Session()
    s.authorize_writes(write_key)
    acc = Account(uid="987654321", account_hash="h2", name="u")
    s.add(acc)
    s.flush()

    t1 = datetime.datetime(2026, 3, 1, 12, 0, 0)
    t2 = datetime.datetime(2026, 3, 1, 12, 3, 0)
    mark_client_started(s, acc, t1, detected_pid=111)
    restarted = mark_client_started(s, acc, t2, detected_pid=222)
    s.commit()

    records = s.query(LoginRecord).filter_by(account_id=acc.id).all()
    assert restarted is True
    assert acc.last_client_start_at == t2
    assert acc.last_client_pid == 222
    assert len(records) == 2
    assert records[-1].is_client_restart is True
    assert records[-1].client_pid == 222
    assert records[-1].client_started_at == t2
    assert abs((records[-1].login_at - local_now()).total_seconds()) < 30
    s.close()


def test_mark_client_started_pid_change_is_restart():
    from src.db import init_db, Account, generate_db_write_key, mark_client_started

    write_key = generate_db_write_key()
    Session = init_db("sqlite:///:memory:", write_key=write_key)
    s = Session()
    s.authorize_writes(write_key)
    acc = Account(uid="555555555", account_hash="h3", name="u", client_enhance=5)
    s.add(acc)
    s.flush()

    dt = datetime.datetime(2026, 3, 1, 12, 0, 0)
    mark_client_started(s, acc, dt, detected_pid=111)
    restarted = mark_client_started(s, acc, dt, detected_pid=222)
    s.commit()

    assert restarted is True
    assert acc.last_client_start_at == dt
    assert acc.last_client_pid == 222
    assert acc.client_enhance == 0
    s.close()


def test_slot_states_show_locked_until_threshold():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    states = runner._build_slot_states(level=0, activated_substats=[], ui_mode="enhance_panel")
    assert states[0]["text"] == "强化至+5可调谐"
    assert states[1]["text"] == "强化至+10可调谐"


def test_slot_states_pending_when_unlocked_without_explicit_current_slot():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    substats = [{"name": "攻击", "value": 8.6, "is_pct": True}]
    states = runner._build_slot_states(level=15, activated_substats=substats, ui_mode="enhance_panel")
    assert states[1]["text"] == "待调谐"
    assert states[2]["text"] == "待调谐"


def test_refresh_locked_uid_on_uid_change():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner._uid_locked = True
    runner._current_uid = "111111111"
    runner._current_account_id = 123
    runner._next_uid_recheck_at = 0.0
    runner._extract_uid_from_frame = lambda _: "222222222"

    runner._maybe_refresh_locked_uid(frame=None)

    assert runner._uid_locked is False
    assert runner._current_uid is None
    assert runner._current_account_id is None
    assert runner._last_uid == "222222222"
    assert runner._uid_consistent_count == 1


def test_uid_lock_confirmations_default_to_two():
    from src.pipeline import GameProcessSnapshot, PipelineRunner

    runner = PipelineRunner()
    runner._uid_lock_confirmations = 2
    runner._extract_uid_from_frame = lambda _: "123456789"
    started_at = datetime.datetime(2026, 3, 1, 12, 0, 0)
    runner._get_game_process_snapshot = lambda: GameProcessSnapshot(
        pid=9876,
        started_at=started_at,
        captured_at=0.0,
    )
    runner._create_or_get_account = lambda uid, client_started_at, client_pid: 42

    first = runner._bind_uid_if_ready(frame=None)
    assert first["uid_locked"] is False
    assert first["uid"] == "123456789"
    assert first["uid_consistent"] == 1
    assert first["uid_required"] == 2

    runner._next_uid_retry_at = 0.0
    second = runner._bind_uid_if_ready(frame=None)
    assert second["uid_locked"] is True
    assert second["uid_consistent"] == 2
    assert second["uid_required"] == 2
    assert runner._current_uid == "123456789"
    assert runner._current_account_id == 42
    assert runner._current_client_started_at == started_at
    assert runner._current_client_pid == 9876


def test_game_process_snapshot_change_clears_uid_binding():
    from src.pipeline import GameProcessSnapshot, PipelineRunner

    runner = PipelineRunner()
    runner._uid_locked = True
    runner._current_uid = "123456789"
    runner._current_account_id = 42
    runner._current_client_pid = 111
    runner._current_client_started_at = datetime.datetime(2026, 3, 1, 12, 0, 0)
    runner._game_process_snapshot = GameProcessSnapshot(
        pid=111,
        started_at=datetime.datetime(2026, 3, 1, 12, 0, 0),
        captured_at=0.0,
    )
    runner._scan_game_process_snapshot = lambda: GameProcessSnapshot(
        pid=222,
        started_at=datetime.datetime(2026, 3, 1, 12, 5, 0),
        captured_at=1.0,
    )

    snapshot = runner._refresh_game_process_snapshot(force=True)

    assert snapshot.pid == 222
    assert runner._uid_locked is False
    assert runner._current_uid is None
    assert runner._current_account_id is None
    assert runner._current_client_pid is None


def test_action_strategy_advisor_prefers_multi_when_multi_score_higher():
    from src.probability import ActionStrategyAdvisor

    advisor = ActionStrategyAdvisor(min_samples_for_confident=2)
    records = [
        {"action_type": "single", "value_tier": 1, "is_historical_unknown": False},
        {"action_type": "single", "value_tier": 1, "is_historical_unknown": False},
        {"action_type": "multi", "value_tier": 4, "is_historical_unknown": False},
        {"action_type": "multi", "value_tier": 3, "is_historical_unknown": False},
    ]
    result = advisor.recommend(records, tunable_slots_count=2)
    assert result.recommended_action == "multi"
    assert result.multi_score > result.single_score


def test_action_strategy_advisor_fallback_without_samples():
    from src.probability import ActionStrategyAdvisor

    advisor = ActionStrategyAdvisor()
    result = advisor.recommend([], tunable_slots_count=1)
    assert result.recommended_action == "single"
    assert result.single_samples == 0
    assert result.multi_samples == 0


def test_substat_posterior_model_uses_context_and_excludes_existing():
    from src.probability import SubstatPosteriorModel

    records = [
        {
            "substat_name": "暴击",
            "substat_value": 6.3,
            "cost": 3,
            "set_name": "逆光跃彩之约",
            "main_stat": "衍射伤害加成",
        },
        {
            "substat_name": "暴击",
            "substat_value": 7.5,
            "cost": 3,
            "set_name": "逆光跃彩之约",
            "main_stat": "衍射伤害加成",
        },
        {
            "substat_name": "防御",
            "substat_value": 50,
            "cost": 4,
            "set_name": "凝夜白霜",
            "main_stat": "暴击",
        },
    ]
    candidates = [
        {"name": "暴击", "is_percent": True},
        {"name": "防御", "is_percent": False},
        {"name": "攻击", "is_percent": True},
    ]

    out = SubstatPosteriorModel(min_layer_samples=1).fit(records).predict(
        {
            "cost": 3,
            "set_name": "逆光跃彩之约",
            "main_stat": "衍射伤害加成",
            "existing_substats": [{"name": "攻击", "value": 7.9, "is_pct": True}],
        },
        candidate_pool=candidates,
    )

    assert out["predictions"][0]["name"] == "暴击"
    assert "攻击%" not in out["probabilities"]


def test_substat_posterior_model_distinguishes_flat_and_percent_attack():
    from src.probability import SubstatPosteriorModel

    records = [
        {"substat_name": "攻击", "substat_value": 8.6, "cost": 3},
        {"substat_name": "攻击", "substat_value": 40, "cost": 3},
    ]
    candidates = [
        {"name": "攻击", "is_percent": True},
        {"name": "攻击", "is_percent": False},
    ]

    out = SubstatPosteriorModel(min_layer_samples=1).fit(records).predict(
        {"cost": 3},
        candidate_pool=candidates,
    )

    assert "攻击%" in out["probabilities"]
    assert "攻击" in out["probabilities"]


def test_strategy_priority_profile_set_override(tmp_path):
    from src.strategy_config import load_strategy_priority_profile

    cfg = {
        "weights": {"high": 1.0, "medium": 0.7, "low": 0.4, "fallback": 0.5},
        "default_priorities": {
            "high": ["暴击"],
            "medium": ["攻击"],
            "low": ["防御"],
            "equal_groups": [["暴击", "暴击伤害"]],
        },
        "set_priorities": {
            "轻云出月": {
                "high": ["共鸣效率"],
                "medium": ["暴击"],
                "low": [],
                "equal_groups": [["暴击", "暴击伤害"]],
            }
        },
    }
    path = tmp_path / "priority.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    profile = load_strategy_priority_profile(path)
    assert profile.weight_for("暴击", "轻云出月") == profile.weight_for("暴击伤害", "轻云出月")
    assert profile.weight_for("共鸣效率", "轻云出月") > profile.weight_for("防御", "轻云出月")


def test_strategy_priority_profile_by_character_and_consonant(tmp_path):
    from src.strategy_config import load_strategy_priority_profile

    cfg = {
        "weights": {"high": 1.0, "medium": 0.7, "low": 0.4, "fallback": 0.5},
        "default_priorities": {
            "high": ["暴击"],
            "medium": ["攻击"],
            "low": ["生命"],
            "equal_groups": [],
        },
        "set_priorities": {
            "隐世回光": {
                "selected_character": "白芷",
                "by_character": {
                    "白芷": {
                        "cost_main_stats": {
                            "COST4": ["生命"],
                            "COST3": ["共鸣效率"],
                            "COST1": ["生命"],
                        },
                        "consonant": {
                            "1": ["生命"],
                            "2": ["共鸣效率"],
                        }
                    },
                    "守岸人": {
                        "consonant": {
                            "1": ["共鸣效率"],
                            "2": ["暴击伤害"],
                            "3": ["生命"],
                        }
                    },
                },
            }
        },
    }
    path = tmp_path / "priority_roles.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    profile = load_strategy_priority_profile(path)
    weights_default, source_default, selected_default, roles = profile.weights_for(set_name="隐世回光")
    assert source_default == "set_role_default"
    assert selected_default == "白芷"
    assert roles == ["白芷", "守岸人"]
    assert weights_default["生命"] > weights_default.get("暴击伤害", 0.0)

    weights_guard, source_guard, selected_guard, _ = profile.weights_for(
        set_name="隐世回光",
        character_name="守岸人",
    )
    assert source_guard == "set_role_override"
    assert selected_guard == "守岸人"
    assert weights_guard["共鸣效率"] > weights_guard["生命"]

    perfect_default, perfect_source, perfect_selected, perfect_roles = profile.perfect_for(set_name="隐世回光")
    assert perfect_source == "set_role_default"
    assert perfect_selected == "白芷"
    assert perfect_roles == ["白芷", "守岸人"]
    assert perfect_default["cost_main_stats"]["COST4"] == ["生命"]
    assert perfect_default["consonant"]["1"] == ["生命"]


def test_pipeline_can_switch_strategy_role(tmp_path):
    from src.pipeline import PipelineRunner

    cfg = {
        "weights": {"high": 1.0, "medium": 0.7, "low": 0.4, "fallback": 0.5},
        "default_priorities": {
            "high": ["暴击"],
            "medium": ["攻击"],
            "low": ["生命"],
            "equal_groups": [],
        },
        "set_priorities": {
            "隐世回光": {
                "selected_character": "白芷",
                "by_character": {
                    "白芷": {"consonant": {"1": ["生命"], "2": ["共鸣效率"]}},
                    "守岸人": {"consonant": {"1": ["共鸣效率"], "2": ["暴击伤害"], "3": ["生命"]}},
                },
            }
        },
    }
    path = tmp_path / "priority_pipeline_roles.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    runner = PipelineRunner(strategy_config_path=str(path))

    _, source_default, selected_default, roles_default = runner._resolve_strategy_role("隐世回光")
    assert source_default == "set_role_default"
    assert selected_default == "白芷"
    assert "守岸人" in roles_default

    assert runner.set_strategy_character_for_set("隐世回光", "守岸人") is True
    weights_switched, source_switched, selected_switched, _ = runner._resolve_strategy_role("隐世回光")
    assert source_switched == "set_role_override"
    assert selected_switched == "守岸人"
    assert weights_switched["共鸣效率"] > weights_switched["生命"]


def test_extract_observation_equipment_from_ocr_row():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    obs = runner._extract_observation(["声骸详情", "白芷装配中", "+0"], [], conf=0.8)
    assert obs.equipment == "白芷"


def test_extract_observation_accepts_intermediate_level():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["长路启航之星"],
        "main_stats": ["热熔伤害加成"],
        "echo_names": ["格洛犸图"],
    }

    obs = runner._extract_observation(
        [
            "格洛犸图 +22",
            "COST3 C 6",
            "热熔伤害加成 27.1%",
            "攻击 90",
            "共鸣解放伤害加成 7.9%",
            "生命 510",
            "攻击 7.9%",
            "待调谐",
            "声骸技能",
            "召唤格洛犸图，对敌人造成冷凝伤",
            "合鸣效果 图",
            "长路启航之星 (1/2)",
        ],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )

    assert obs.level == 22
    assert obs.cost == 3
    assert obs.echo_name == "格洛犸图"
    assert obs.set_name == "长路启航之星"
    assert obs.main_stat == "热熔伤害加成"
    assert obs.slot_states[3]["status"] != "locked_by_level"
    assert obs.slot_states[4]["text"] == "强化至+25可调谐"


def test_enhance_panel_uses_panel_specific_rows_and_slot_texts():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["长路启航之星"],
        "main_stats": ["热熔伤害加成"],
        "echo_names": ["格洛犸图"],
    }
    runner.observation_extractor.substat_values = [
        {"name": "共鸣解放伤害加成", "is_percent": True, "alias": [], "values": [7.9]},
        {"name": "生命", "is_percent": False, "alias": [], "values": [510]},
        {"name": "攻击", "is_percent": True, "alias": [], "values": [7.9]},
    ]
    runner.observation_extractor._substat_index = runner.observation_extractor._build_substat_index()

    obs = runner._extract_observation(
        [
            "声骸强化",
            "格洛犸图",
            "热熔伤害加成 27.1%",
            "攻击 90",
            "共鸣解放伤害加成 7.9%",
            "生命 510",
            "攻击 7.9%",
            "待调谐",
            "强化至+25可调谐",
        ],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )

    assert obs.level is None
    assert obs.echo_name == "格洛犸图"
    assert obs.main_stat == "热熔伤害加成"
    assert obs.set_name == ""
    assert obs.substats[0]["name"] == "共鸣解放伤害加成"
    assert obs.slot_states[0]["status"] == "activated"
    assert obs.slot_states[1]["name"] == "生命"
    assert obs.slot_states[2]["name"] == "攻击"
    assert obs.slot_states[3]["text"] == "待调谐"
    assert obs.slot_states[4]["text"] == "强化至+25可调谐"


def test_enhance_panel_handles_missing_title_row_without_shift():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["长路启航之星"],
        "main_stats": ["热熔伤害加成"],
        "echo_names": ["格洛犸图"],
    }
    runner.observation_extractor.substat_values = [
        {"name": "共鸣解放伤害加成", "is_percent": True, "alias": [], "values": [7.9]},
        {"name": "生命", "is_percent": False, "alias": [], "values": [510]},
        {"name": "攻击", "is_percent": True, "alias": [], "values": [7.9]},
    ]
    runner.observation_extractor._substat_index = runner.observation_extractor._build_substat_index()

    obs = runner._extract_observation(
        [
            "格洛犸图",
            "热熔伤害加成 27.1%",
            "X攻击 90",
            "共鸣解放伤害加成 7.9%",
            "生命 510",
            "攻击 7.9%",
            "激活新辅音属性",
            "强化至+25可调谐",
        ],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )

    assert obs.echo_name == "格洛犸图"
    assert obs.main_stat == "热熔伤害加成"
    assert obs.substats[0]["name"] == "共鸣解放伤害加成"
    assert obs.slot_states[0]["name"] == "共鸣解放伤害加成"
    assert obs.slot_states[3]["text"] == "激活新辅音属性"
    assert obs.slot_states[4]["text"] == "强化至+25可调谐"


def test_enhance_panel_ocr_typo_main_stat_keeps_slot_alignment():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["逆光跃彩之约"],
        "main_stats": ["衍射伤害加成", "攻击"],
        "echo_names": ["锯袭铁影"],
    }
    runner.observation_extractor.substat_values = [
        {"name": "攻击", "is_percent": False, "alias": [], "values": [52]},
    ]
    runner.observation_extractor._substat_index = runner.observation_extractor._build_substat_index()

    obs = runner._extract_observation(
        [
            "周谐",
            "锯袭铁影 中",
            "行射伤害加成 15.6%",
            "攻击 52",
            "激活新辅音属性",
            "待调谐",
            "强化至+15可调谐",
            "强化至+20可调谐",
            "强化至+25可调谐",
        ],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )

    assert obs.echo_name == "锯袭铁影"
    assert obs.main_stat == "衍射伤害加成"
    assert obs.substats == []
    assert [state["text"] for state in obs.slot_states] == [
        "激活新辅音属性",
        "待调谐",
        "强化至+15可调谐",
        "强化至+20可调谐",
        "强化至+25可调谐",
    ]


def test_enhance_panel_matches_echo_anchor_and_inherits_identity():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["长路启航之星"],
        "main_stats": ["热熔伤害加成"],
        "echo_names": ["格洛犸图"],
    }
    runner.observation_extractor.substat_values = [
        {"name": "共鸣解放伤害加成", "is_percent": True, "alias": [], "values": [7.9]},
        {"name": "生命", "is_percent": False, "alias": [], "values": [510]},
        {"name": "攻击", "is_percent": True, "alias": [], "values": [7.9]},
    ]
    runner.observation_extractor._substat_index = runner.observation_extractor._build_substat_index()

    detail = runner._extract_observation(
        [
            "格洛犸图 +22",
            "COST3",
            "热熔伤害加成 27.1%",
            "攻击 90",
            "共鸣解放伤害加成 7.9%",
            "生命 510",
            "攻击 7.9%",
            "待调谐",
            "声骸技能",
            "召唤格洛犸图",
            "合鸣效果",
            "长路启航之星 (1/2)",
        ],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    runner._resolve_active_echo_observation(detail, scene="echo_panel", now=1.0)

    enhance = runner._extract_observation(
        [
            "声骸强化",
            "格洛犸图",
            "热熔伤害加成 27.1%",
            "攻击 90",
            "共鸣解放伤害加成 7.9%",
            "生命 510",
            "攻击 7.9%",
            "待调谐",
            "强化至+25可调谐",
        ],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )
    resolved = runner._resolve_active_echo_observation(enhance, scene="enhance_panel", now=2.0)

    assert resolved.echo_name == "格洛犸图"
    assert resolved.level == 22
    assert resolved.cost == 3
    assert resolved.set_name == "长路启航之星"
    assert resolved.main_stat == "热熔伤害加成"
    assert resolved.slot_states[4]["text"] == "强化至+25可调谐"


def test_same_echo_name_hard_conflict_does_not_replace_detail_anchor():
    from src.observation_extractor import EchoObservation
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    detail = EchoObservation(
        level=22,
        cost=3,
        set_name="长路启航之星",
        main_stat="热熔伤害加成",
        echo_name="格洛犸图",
        equipment=None,
        is_locked=None,
        substats=[{"name": "共鸣解放伤害加成", "value": 7.9, "is_pct": True}],
        source_region="test",
        ocr_confidence=1.0,
        ui_mode="echo_panel",
        slot_states=[],
    )
    runner._resolve_active_echo_observation(detail, scene="echo_panel", now=1.0)

    bad_enhance = EchoObservation(
        level=None,
        cost=None,
        set_name="",
        main_stat="攻击%",
        echo_name="格洛犸图",
        equipment=None,
        is_locked=None,
        substats=[{"name": "生命", "value": 510.0, "is_pct": False}],
        source_region="test",
        ocr_confidence=1.0,
        ui_mode="enhance_panel",
        slot_states=[],
    )

    for idx in range(4):
        resolved = runner._resolve_active_echo_observation(bad_enhance, scene="enhance_panel", now=2.0 + idx)

    assert resolved.echo_name == "格洛犸图"
    assert resolved.set_name == "长路启航之星"
    assert resolved.main_stat == "热熔伤害加成"
    assert resolved.level == 22


def test_active_echo_context_ignores_empty_observation():
    from src.observation_extractor import EchoObservation
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    detail = EchoObservation(
        level=22,
        cost=3,
        set_name="长路启航之星",
        main_stat="热熔伤害加成",
        echo_name="格洛犸图",
        equipment=None,
        is_locked=None,
        substats=[{"name": "共鸣解放伤害加成", "value": 7.9, "is_pct": True}],
        source_region="test",
        ocr_confidence=1.0,
        ui_mode="echo_panel",
        slot_states=[
            {
                "slot_index": 1,
                "status": "activated",
                "text": "共鸣解放伤害加成 7.9%",
                "name": "共鸣解放伤害加成",
                "value": 7.9,
                "is_pct": True,
            }
        ],
    )
    runner._resolve_active_echo_observation(detail, scene="echo_panel", now=1.0)

    empty = EchoObservation(
        level=None,
        cost=None,
        set_name="",
        main_stat="",
        echo_name="",
        equipment=None,
        is_locked=None,
        substats=[],
        source_region="test",
        ocr_confidence=1.0,
        ui_mode="enhance_panel",
        slot_states=[],
    )
    resolved = runner._resolve_active_echo_observation(empty, scene="enhance_panel", now=2.0)

    assert resolved.echo_name == "格洛犸图"
    assert resolved.set_name == "长路启航之星"
    assert resolved.main_stat == "热熔伤害加成"
    assert resolved.substats == detail.substats
    assert resolved.slot_states == detail.slot_states


def test_pipeline_prefers_equipment_strategy_role(tmp_path):
    from src.pipeline import PipelineRunner

    cfg = {
        "weights": {"high": 1.0, "medium": 0.7, "low": 0.4, "fallback": 0.5},
        "default_priorities": {
            "high": ["暴击"],
            "medium": ["攻击"],
            "low": ["生命"],
            "equal_groups": [],
        },
        "set_priorities": {
            "隐世回光": {
                "selected_character": "白芷",
                "by_character": {
                    "白芷": {"consonant": {"1": ["生命"], "2": ["共鸣效率"]}},
                    "守岸人": {"consonant": {"1": ["共鸣效率"], "2": ["暴击伤害"], "3": ["生命"]}},
                },
            }
        },
    }
    path = tmp_path / "priority_equipment_roles.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    runner = PipelineRunner(strategy_config_path=str(path))
    weights, source, selected, _ = runner._resolve_strategy_role("隐世回光", equipment="守岸人")
    assert source == "set_role_override"
    assert selected == "守岸人"
    assert weights["共鸣效率"] > weights["生命"]


def test_action_strategy_advisor_priority_weight_affects_recommendation():
    from src.probability import ActionStrategyAdvisor

    advisor = ActionStrategyAdvisor(min_samples_for_confident=1)
    records = [
        {"action_type": "single", "value_tier": 3, "is_historical_unknown": False, "substat_name": "暴击"},
        {"action_type": "single", "value_tier": 3, "is_historical_unknown": False, "substat_name": "暴击"},
        {"action_type": "multi", "value_tier": 4, "is_historical_unknown": False, "substat_name": "防御"},
        {"action_type": "multi", "value_tier": 4, "is_historical_unknown": False, "substat_name": "防御"},
    ]
    priority_weights = {"暴击": 1.0, "防御": 0.1}

    result = advisor.recommend(records, tunable_slots_count=2, priority_weights=priority_weights)
    assert result.recommended_action == "single"


def test_action_strategy_advisor_treats_attack_and_defense_priority_as_percent_only():
    from src.probability import ActionStrategyAdvisor

    priority_weights = {"攻击": 1.0, "防御": 1.0}

    flat_weight = ActionStrategyAdvisor._priority_weight(
        {"substat_name": "攻击", "substat_value": 40, "is_pct": False},
        priority_weights,
        fallback=0.5,
    )
    pct_weight = ActionStrategyAdvisor._priority_weight(
        {"substat_name": "攻击", "substat_value": 8.6, "is_pct": True},
        priority_weights,
        fallback=0.5,
    )
    flat_def_weight = ActionStrategyAdvisor._priority_weight(
        {"substat_name": "防御", "substat_value": 40, "is_pct": False},
        priority_weights,
        fallback=0.5,
    )
    pct_def_weight = ActionStrategyAdvisor._priority_weight(
        {"substat_name": "防御", "substat_value": 8.6, "is_pct": True},
        priority_weights,
        fallback=0.5,
    )

    assert flat_weight == 0.5
    assert pct_weight == 1.0
    assert flat_def_weight == 0.5
    assert pct_def_weight == 1.0


def test_action_strategy_advisor_cost_analysis_switches_bad_three_slot_echo():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "生命", "value": 510, "is_pct": False},
            {"name": "防御", "value": 50, "is_pct": False},
            {"name": "攻击", "value": 40, "is_pct": False},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        fallback=0.5,
    )

    assert analysis["recommended_action"] == "switch_echo"
    assert analysis["stop_recommended"] is True
    assert analysis["first_three_top_hit_count"] == 0
    assert "换下一只声骸" in analysis["suggestions"]


def test_action_strategy_advisor_cost_analysis_drops_when_first_two_are_invalid():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "生命", "value": 510, "is_pct": False},
            {"name": "防御", "value": 50, "is_pct": False},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        fallback=0.5,
    )

    assert analysis["first_two_effective_hit_count"] == 0
    assert analysis["recommended_action"] == "switch_echo"
    assert "直接放弃当前声骸" in analysis["suggestions"]


def test_action_strategy_advisor_cost_analysis_finishes_full_echo():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "暴击", "value": 6.3, "is_pct": True},
            {"name": "暴击伤害", "value": 12.6, "is_pct": True},
            {"name": "攻击", "value": 8.6, "is_pct": True},
            {"name": "共鸣效率", "value": 8.4, "is_pct": True},
            {"name": "防御", "value": 8.6, "is_pct": True},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        fallback=0.5,
    )

    assert analysis["recommended_action"] == "finished"
    assert analysis["remaining_slots"] == 0
    assert analysis["top_hit_count"] == 2


def test_action_strategy_advisor_cost_analysis_drops_two_of_four_effective_echo():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "暴击", "value": 6.3, "is_pct": True},
            {"name": "生命", "value": 510, "is_pct": False},
            {"name": "攻击", "value": 8.6, "is_pct": True},
            {"name": "防御", "value": 50, "is_pct": False},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        fallback=0.5,
    )

    assert analysis["effective_hit_count"] == 2
    assert analysis["recommended_action"] == "switch_echo"
    assert "成本过高" in analysis["reason"]


def test_action_strategy_advisor_parks_good_echo_when_next_hit_probability_is_low():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "暴击", "value": 6.3, "is_pct": True},
            {"name": "暴击伤害", "value": 12.6, "is_pct": True},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        substat_posterior={
            "probabilities": {
                "生命": 0.55,
                "防御": 0.20,
                "攻击%": 0.10,
                "共鸣效率": 0.05,
                "防御%": 0.05,
                "共鸣解放伤害加成": 0.05,
            }
        },
        fallback=0.5,
    )

    assert analysis["top_hit_count"] == 2
    assert analysis["next_effective_probability"] < 0.35
    assert analysis["recommended_action"] == "park_echo"
    assert "先换另一个声骸强化" in analysis["suggestions"]


def test_action_strategy_advisor_continues_good_echo_when_next_hit_probability_is_high():
    from src.probability import ActionStrategyAdvisor

    analysis = ActionStrategyAdvisor.analyze_cost(
        [
            {"name": "暴击", "value": 6.3, "is_pct": True},
            {"name": "暴击伤害", "value": 12.6, "is_pct": True},
        ],
        perfect_consonant={
            "1": ["暴击"],
            "2": ["暴击伤害"],
            "3": ["攻击"],
            "4": ["共鸣效率"],
            "5": ["防御"],
        },
        priority_weights={"暴击": 1.0, "暴击伤害": 1.0, "攻击": 0.7, "共鸣效率": 0.7, "防御": 0.4},
        substat_posterior={
            "probabilities": {
                "攻击%": 0.35,
                "共鸣效率": 0.20,
                "防御%": 0.10,
                "生命": 0.20,
                "共鸣解放伤害加成": 0.15,
            }
        },
        fallback=0.5,
    )

    assert analysis["next_effective_probability"] >= 0.55
    assert analysis["recommended_action"] == "continue_echo"
    assert "继续强化当前声骸" in analysis["suggestions"]


def test_strategy_config_invalid_json_reports_errors_and_fallback(tmp_path):
    from src.strategy_config import load_strategy_priority_profile_with_meta

    bad_file = tmp_path / "bad_priority.json"
    bad_file.write_text("{bad-json", encoding="utf-8")

    res = load_strategy_priority_profile_with_meta(bad_file)
    assert res.used_default is True
    assert res.errors
    assert "解析失败" in res.errors[0]


def test_strategy_config_valid_no_errors(tmp_path):
    from src.strategy_config import load_strategy_priority_profile_with_meta

    cfg = {
        "weights": {"high": 1.0, "medium": 0.7, "low": 0.4, "fallback": 0.5},
        "default_priorities": {
            "high": ["暴击"],
            "medium": ["攻击"],
            "low": ["防御"],
            "equal_groups": [["暴击", "暴击伤害"]],
        },
        "set_priorities": {},
    }
    path = tmp_path / "priority_ok.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    res = load_strategy_priority_profile_with_meta(path)
    assert res.used_default is False
    assert res.errors == []


def test_extract_observation_uses_echo_dictionary():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["凝夜白霜"],
        "main_stats": ["暴击"],
        "echo_names": ["无妄者"],
    }
    raw_texts = ["无妄者", "凝夜白霜", "主属性 暴击", "COST 4", "+10", "待调谐"]
    parsed = [{"name": "暴击", "value": 6.3, "is_pct": True}]

    obs = runner._extract_observation(raw_texts, parsed, conf=0.8, ui_mode_override="echo_panel")
    assert obs.set_name == "凝夜白霜"
    assert obs.main_stat == "暴击"
    assert obs.echo_name == "无妄者"
    assert obs.cost == 4
    assert obs.level == 10


def test_extract_observation_dictionary_fallback_unknown():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {"set_names": [], "main_stats": [], "echo_names": []}
    obs = runner._extract_observation(["随机文字", "+5"], [], conf=0.5)
    assert obs.set_name == ""
    assert obs.main_stat == ""
    assert obs.echo_name == ""


def test_extract_observation_returns_current_identity_without_stabilization():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["凝夜白霜", "轻云出月"],
        "main_stats": ["暴击", "攻击"],
        "echo_names": ["无妄者", "辉萤军势"],
    }

    first = runner._extract_observation(["无妄者", "COST 4", "暴击", "凝夜白霜"], [], conf=0.8)
    assert first.echo_name == "无妄者"
    assert first.set_name == "凝夜白霜"
    assert first.main_stat == "暴击"

    one_mismatch = runner._extract_observation(["辉萤军势", "COST 3", "攻击", "轻云出月"], [], conf=0.8)
    assert one_mismatch.echo_name == "辉萤军势"
    assert one_mismatch.set_name == "轻云出月"
    assert one_mismatch.main_stat == "攻击"
    assert one_mismatch.cost == 3


def test_extract_observation_empty_result_is_not_held_by_extractor():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["凝夜白霜"],
        "main_stats": ["暴击"],
        "echo_names": ["无妄者"],
    }

    first = runner._extract_observation(["无妄者", "COST 4", "暴击", "凝夜白霜"], [], conf=0.8)
    assert first.echo_name == "无妄者"

    one_missing = runner._extract_observation(["随机文字"], [], conf=0.8)
    assert one_missing.echo_name == ""
    assert one_missing.set_name == ""
    assert one_missing.main_stat == ""


def test_echo_panel_force_accepts_new_echo_attrs():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    runner.echo_dictionary = {
        "set_names": ["凝夜白霜", "轻云出月"],
        "main_stats": ["暴击", "攻击"],
        "echo_names": ["无妄者", "辉萤军势"],
    }

    first = runner._extract_observation(
        ["无妄者", "COST 4", "暴击", "凝夜白霜"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    assert first.echo_name == "无妄者"

    detail_switch = runner._extract_observation(
        ["辉萤军势", "COST 3", "攻击", "轻云出月"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    assert detail_switch.echo_name == "辉萤军势"
    assert detail_switch.set_name == "轻云出月"
    assert detail_switch.main_stat == "攻击"


def test_active_echo_context_keeps_identity_on_first_enhance_mismatch():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    anchor = runner.observation_extractor.extract_observation(
        ["无妄者 +5", "COST 4", "暴击", "凝夜白霜"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    anchor = runner._resolve_active_echo_observation(anchor, scene="echo_panel", now=1.0)

    mismatch = runner.observation_extractor.extract_observation(
        ["辉萤军势 +10", "COST 3", "攻击", "轻云出月"],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )
    resolved = runner._resolve_active_echo_observation(mismatch, scene="enhance_panel", now=2.0)

    assert resolved.echo_name == anchor.echo_name
    assert resolved.set_name == anchor.set_name
    assert resolved.main_stat == anchor.main_stat
    assert resolved.level == anchor.level


def test_active_echo_context_replaces_only_from_detail_panel():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    anchor = runner.observation_extractor.extract_observation(
        ["无妄者", "COST 4", "暴击", "凝夜白霜"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    runner._resolve_active_echo_observation(anchor, scene="echo_panel", now=1.0)

    first_mismatch = runner.observation_extractor.extract_observation(
        ["辉萤军势", "COST 3", "攻击", "轻云出月"],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )
    runner._resolve_active_echo_observation(first_mismatch, scene="enhance_panel", now=2.0)

    second_mismatch = runner.observation_extractor.extract_observation(
        ["辉萤军势", "COST 3", "攻击", "轻云出月"],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )
    resolved = runner._resolve_active_echo_observation(second_mismatch, scene="enhance_panel", now=3.0)

    assert resolved.echo_name == "无妄者"
    assert resolved.set_name == "凝夜白霜"
    assert resolved.main_stat == "暴击"

    detail_switch = runner.observation_extractor.extract_observation(
        ["辉萤军势", "COST 3", "攻击", "轻云出月"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    resolved = runner._resolve_active_echo_observation(detail_switch, scene="echo_panel", now=4.0)

    assert resolved.echo_name == "辉萤军势"
    assert resolved.set_name == "轻云出月"
    assert resolved.main_stat == "攻击"


def test_active_echo_context_inherits_level_when_enhance_panel_hides_it():
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    anchor = runner.observation_extractor.extract_observation(
        ["无妄者 +5", "COST 4", "暴击", "凝夜白霜"],
        [],
        conf=0.8,
        ui_mode_override="echo_panel",
    )
    runner._resolve_active_echo_observation(anchor, scene="echo_panel", now=1.0)

    same_echo = runner.observation_extractor.extract_observation(
        ["声骸强化", "无妄者", "暴击", "", "待调谐"],
        [],
        conf=0.8,
        ui_mode_override="enhance_panel",
    )
    resolved = runner._resolve_active_echo_observation(same_echo, scene="enhance_panel", now=2.0)

    assert resolved.echo_name == "无妄者"
    assert resolved.set_name == "凝夜白霜"
    assert resolved.level == 5


def test_active_echo_context_local_id_controls_db_session():
    from sqlalchemy import inspect as sa_inspect

    from src.db import Account, EchoSubstat, init_db
    from src.observation_extractor import EchoObservation
    from src.pipeline import PipelineRunner

    def make_obs(
        echo_name: str,
        cost: int,
        set_name: str,
        main_stat: str,
        level: int,
        ui_mode: str,
        substats=None,
    ) -> EchoObservation:
        return EchoObservation(
            level=level,
            cost=cost,
            set_name=set_name,
            main_stat=main_stat,
            echo_name=echo_name,
            equipment=None,
            is_locked=None,
            substats=list(substats or []),
            source_region="test",
            ocr_confidence=1.0,
            ui_mode=ui_mode,
            slot_states=[],
        )

    runner = PipelineRunner(db_path="sqlite:///:memory:")
    Session = init_db("sqlite:///:memory:", write_key=runner._db_write_key)
    s = Session()
    s.authorize_writes(runner._db_write_key)
    inspector = sa_inspect(s.bind)
    assert "enhance_actions" not in inspector.get_table_names()
    assert "enhance_events" not in inspector.get_table_names()
    assert "echo_substats" in inspector.get_table_names()
    assert "samples" not in inspector.get_table_names()
    assert "substat_definitions" not in inspector.get_table_names()
    echo_substat_columns = {c["name"]: c for c in inspector.get_columns("echo_substats")}
    assert "id" in echo_substat_columns
    assert inspector.get_pk_constraint("echo_substats").get("constrained_columns") == ["id"]

    acc = Account(uid="123456789", account_hash="h", name="u")
    s.add(acc)
    s.flush()

    now = datetime.datetime(2026, 3, 1, 12, 0, 0)
    anchor = runner._resolve_active_echo_observation(
        make_obs("无妄者", 4, "凝夜白霜", "暴击", 5, "echo_panel"),
        scene="echo_panel",
        now=1.0,
    )
    assert runner._resolve_session(s, acc.id, anchor, now) is None

    same_echo = runner._resolve_active_echo_observation(
        make_obs(
            "无妄者",
            4,
            "凝夜白霜",
            "暴击",
            10,
            "enhance_panel",
            substats=[{"name": "攻击", "value": 8.6, "is_pct": True}],
        ),
        scene="enhance_panel",
        now=2.0,
    )
    session1 = runner._resolve_session(s, acc.id, same_echo, now)
    assert session1 is not None
    assert len(session1.echo_instance_id) == 16
    assert session1.initial_substat_count == 1
    runner._persist_action_events(s, acc.id, session1, same_echo, now)
    s.flush()
    s.refresh(acc)
    first_event = s.query(EchoSubstat).filter_by(account_id=acc.id, session_id=session1.session_id, slot_index=1).one()
    assert isinstance(first_event.id, int)
    assert first_event.id >= 1
    assert first_event.action_type == "history"
    assert first_event.action_open_count == 1
    assert acc.total_enhance == None or acc.total_enhance == 0
    assert acc.today_enhance == None or acc.today_enhance == 0
    assert acc.client_enhance == None or acc.client_enhance == 0

    same_echo_more_substats = runner._resolve_active_echo_observation(
        make_obs(
            "无妄者",
            4,
            "凝夜白霜",
            "暴击",
            15,
            "enhance_panel",
            substats=[
                {"name": "攻击", "value": 8.6, "is_pct": True},
                {"name": "暴击", "value": 6.3, "is_pct": True},
            ],
        ),
        scene="enhance_panel",
        now=2.5,
    )
    session2 = runner._resolve_session(s, acc.id, same_echo_more_substats, now)
    assert session2 is not None
    assert session2.session_id == session1.session_id
    runner._persist_action_events(s, acc.id, session2, same_echo_more_substats, now)
    s.flush()
    s.refresh(acc)
    second_event = s.query(EchoSubstat).filter_by(account_id=acc.id, session_id=session1.session_id, slot_index=2).one()
    assert isinstance(second_event.id, int)
    assert second_event.id > first_event.id
    assert second_event.action_type == "single"
    assert second_event.action_open_count == 1
    assert acc.total_enhance == 1
    assert acc.today_enhance == 1
    assert acc.client_enhance == 1

    first_mismatch = runner._resolve_active_echo_observation(
        make_obs(
            "辉萤军势",
            3,
            "轻云出月",
            "攻击",
            10,
            "enhance_panel",
            substats=[{"name": "共鸣效率", "value": 8.4, "is_pct": True}],
        ),
        scene="enhance_panel",
        now=3.0,
    )
    session_still_old = runner._resolve_session(s, acc.id, first_mismatch, now)
    assert session_still_old is not None
    assert session_still_old.session_id == session1.session_id

    confirmed_mismatch = runner._resolve_active_echo_observation(
        make_obs(
            "辉萤军势",
            3,
            "轻云出月",
            "攻击",
            10,
            "enhance_panel",
            substats=[{"name": "共鸣效率", "value": 8.4, "is_pct": True}],
        ),
        scene="enhance_panel",
        now=4.0,
    )
    session_still_old = runner._resolve_session(s, acc.id, confirmed_mismatch, now)
    assert session_still_old is not None
    assert session_still_old.session_id == session1.session_id

    detail_switch = runner._resolve_active_echo_observation(
        make_obs(
            "辉萤军势",
            3,
            "轻云出月",
            "攻击",
            10,
            "echo_panel",
            substats=[{"name": "共鸣效率", "value": 8.4, "is_pct": True}],
        ),
        scene="echo_panel",
        now=5.0,
    )
    session3 = runner._resolve_session(s, acc.id, detail_switch, now)
    assert session3 is not None
    assert session3.session_id != session1.session_id
    next_day = datetime.datetime(2026, 3, 2, 4, 1, 0)
    runner._persist_action_events(s, acc.id, session3, detail_switch, next_day)
    s.flush()
    s.refresh(acc)
    assert acc.total_enhance == 1
    assert acc.today_enhance == 1
    assert acc.client_enhance == 1
    s.close()


def test_echo_substats_legacy_event_pk_migrates_to_autoincrement_id(tmp_path):
    from sqlalchemy import create_engine, inspect as sa_inspect, text

    from src.db import generate_db_write_key, init_db

    db_file = tmp_path / "legacy_echo_substats.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, uid VARCHAR(32) NOT NULL UNIQUE)"))
        conn.execute(
            text(
                """
                CREATE TABLE echo_info (
                    account_id INTEGER NOT NULL,
                    echo_instance_id VARCHAR(64) NOT NULL,
                    uid VARCHAR(32) NOT NULL,
                    echo_name VARCHAR(100) NOT NULL,
                    cost INTEGER NOT NULL,
                    set_name VARCHAR(100) NOT NULL,
                    main_stat VARCHAR(100) NOT NULL,
                    initial_substat_count INTEGER NOT NULL,
                    created_at DATETIME,
                    PRIMARY KEY (account_id, echo_instance_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE echo_substats (
                    event_id VARCHAR(64) PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    account_id INTEGER NOT NULL,
                    slot_index INTEGER,
                    substat_name VARCHAR(100) NOT NULL,
                    substat_value FLOAT NOT NULL,
                    created_at DATETIME
                )
                """
            )
        )
        conn.execute(text("INSERT INTO accounts (id, uid) VALUES (1, '123456789')"))
        conn.execute(
            text(
                """
                INSERT INTO echo_info (
                    account_id, echo_instance_id, uid, echo_name, cost, set_name,
                    main_stat, initial_substat_count, created_at
                )
                VALUES (1, 'sess1', '123456789', 'echo', 3, 'set', 'main', 1, '2026-01-01 00:00:00')
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO echo_substats (
                    event_id, session_id, account_id, slot_index,
                    substat_name, substat_value, created_at
                )
                VALUES
                    ('evt-a', 'sess1', 1, 1, '暴击', 6.3, '2026-01-01 00:00:01'),
                    ('evt-b', 'sess1', 1, 2, '攻击', 7.9, '2026-01-01 00:00:02')
                """
            )
        )

    write_key = generate_db_write_key()
    Session = init_db(f"sqlite:///{db_file}", write_key=write_key)
    s = Session()
    try:
        inspector = sa_inspect(s.bind)
        assert inspector.get_pk_constraint("echo_substats").get("constrained_columns") == ["id"]
        rows = s.execute(text("SELECT id, event_id FROM echo_substats ORDER BY id")).fetchall()
        assert [tuple(row) for row in rows] == [(1, "evt-a"), (2, "evt-b")]
    finally:
        s.close()


def test_echo_dictionary_hot_reload(tmp_path):
    from src.pipeline import PipelineRunner

    dict_path = tmp_path / "echo_dict.json"
    dict_path.write_text(
        json.dumps(
            {
                "set_names": ["凝夜白霜"],
                "main_stats": ["暴击"],
                "echo_names": ["无妄者"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runner = PipelineRunner(echo_dictionary_path=str(dict_path))
    obs1 = runner._extract_observation(["无妄者", "凝夜白霜", "暴击"], [], conf=0.8)
    assert obs1.echo_name == "无妄者"

    dict_path.write_text(
        json.dumps(
            {
                "set_names": ["轻云出月"],
                "main_stats": ["攻击"],
                "echo_names": ["辉萤军势"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    runner._reload_echo_dictionary_if_needed(force=True)
    obs2 = runner._extract_observation(["辉萤军势", "轻云出月", "攻击"], [], conf=0.8)
    assert obs2.echo_name == "辉萤军势"
    assert obs2.set_name == "轻云出月"
    assert obs2.main_stat == "攻击"


def test_echo_view_returns_structured_panel_only():
    from src.observation_extractor import EchoObservation
    from src.pipeline import PipelineRunner

    runner = PipelineRunner()
    obs = EchoObservation(
        level=10,
        cost=4,
        set_name="凝夜白霜",
        main_stat="暴击",
        echo_name="无妄者",
        equipment="白芷",
        is_locked=None,
        substats=[{"name": "攻击", "value": 8.6, "is_pct": True, "quality": "high"}],
        source_region="panel_center",
        ocr_confidence=0.9,
        ui_mode="enhance_panel",
        slot_states=[
            {"slot_index": 1, "status": "activated", "text": "攻击 8.6%"},
            {"slot_index": 2, "status": "current_tunable", "text": "激活新辅音属性"},
        ],
    )

    view = runner._build_echo_view(
        obs,
        {
            "recommended_action": "multi",
            "reason": "当前多开样本表现更好",
            "single_score": 0.3,
            "multi_score": 0.8,
            "perfect_cost_main_stats": {"COST4": ["暴击", "暴击伤害"]},
            "perfect_consonant": {"1": ["暴击", "暴击伤害"], "2": ["攻击"]},
            "substat_probabilities": {"攻击": 0.25, "暴击": 0.125},
            "substat_probability_samples": 8,
            "substat_posterior": {
                "predictions": [
                    {"name": "暴击", "probability": 0.4},
                    {"name": "攻击%", "probability": 0.2},
                ],
                "samples": {"global": 8},
            },
        },
        echo_instance_id="abc123def4567890",
    )

    assert view["echo_instance_id"] == "abc123def4567890"
    assert view["echo_name"] == "无妄者"
    assert view["level_text"] == "+10"
    assert view["set_name"] == "凝夜白霜"
    assert view["main_stat"] == "暴击"
    assert len(view["slots"]) == 5
    assert view["slots"][0]["name"] == "攻击"
    assert view["slots"][0]["value_text"] == "8.6%"
    assert view["slots"][0]["probability_text"] == "25.0%"
    assert view["slots"][1]["text"] == "激活新辅音属性"
    assert view["advice"]["text"] == "多开"
    assert view["perfect_recommendation"]["cost_main_stats"]["COST4"] == ["暴击", "暴击伤害"]
    assert view["perfect_recommendation"]["consonant"]["1"] == ["暴击", "暴击伤害"]
    assert view["substat_probabilities"]["攻击"] == 0.25
    assert view["substat_posterior"]["predictions"][0]["name"] == "暴击"


def test_compact_detection_for_ui_removes_ocr_payload():
    from src.pipeline import PipelineRunner

    result = {
        "bbox": [1, 2, 3, 4],
        "conf": 0.9,
        "scene": "enhance_panel",
        "ui_mode": "enhance_panel",
        "raw_texts": ["OCR 原文"],
        "parsed": [{"name": "攻击", "value": 8.6, "is_pct": True}],
        "freq_prob": {"攻击": 1.0},
        "echo": {"echo_name": "无妄者"},
    }
    compact = PipelineRunner._compact_detection_for_ui(result)

    assert compact["echo"]["echo_name"] == "无妄者"
    assert "raw_texts" not in compact
    assert "parsed" not in compact
    assert "freq_prob" not in compact


def test_select_echo_for_output_keeps_last_echo_when_current_detection_empty():
    from src.pipeline import PipelineRunner

    last_echo = {
        "echo_name": "格洛犸图",
        "set_name": "长路启航之星",
        "main_stat": "热熔伤害加成",
    }

    selected = PipelineRunner._select_echo_for_output(
        [{"scene": "enhance_panel", "echo": None}],
        last_echo,
    )

    assert selected == last_echo


def test_select_echo_for_output_keeps_last_echo_when_scene_unknown():
    from src.pipeline import PipelineRunner

    last_echo = {
        "echo_name": "海维夏",
        "set_name": "逆光跃彩之约",
        "main_stat": "暴击伤害",
    }

    selected = PipelineRunner._select_echo_for_output([], last_echo)

    assert selected == last_echo


def test_select_echo_for_output_ignores_empty_echo_view():
    from src.pipeline import PipelineRunner

    last_echo = {"echo_name": "海维夏", "set_name": "逆光跃彩之约"}
    empty_echo = {"echo_name": "", "set_name": "", "main_stat": "", "cost": None, "slots": []}

    selected = PipelineRunner._select_echo_for_output([{"echo": empty_echo}], last_echo)

    assert selected == last_echo


def test_detect_feature_code_returns_uid_value():
    from src.detect_feature_code import detect_uid_value

    class FakeOCR:
        def recognize(self, _img):
            return ["特征码:123456789"]

    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    uid = detect_uid_value(frame=frame, ocr_engine=FakeOCR())
    assert uid == "123456789"


def test_detect_feature_code_returns_bool():
    from src.detect_feature_code import detect_uid_bool

    class FakeOCR:
        def recognize(self, _img):
            return ["无数字"]

    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    ok = detect_uid_bool(frame=frame, ocr_engine=FakeOCR())
    assert ok is False


def test_load_character_entry_map_reads_valid_pairs(tmp_path):
    from tools.fetch_echo_set_recommend import _load_character_entry_map

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "character_name_entry_id.json").write_text(
        json.dumps(
            {
                "白芷": "1347344108314509312",
                "": "1340313641974501376",
                "守岸人": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mapping = _load_character_entry_map(tmp_path)
    assert mapping == {"白芷": "1347344108314509312"}


def test_build_echo_set_recommend_all_dispatches_from_character_map(tmp_path):
    import tools.fetch_echo_set_recommend as mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "character_name_entry_id.json").write_text(
        json.dumps(
            {
                "白芷": "111",
                "守岸人": "222",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    calls = []

    def fake_build(entry_id, project_root, timeout, update_strategy, dry_run):
        calls.append((entry_id, project_root, timeout, update_strategy, dry_run))
        if entry_id == "222":
            raise ValueError("mock failure")
        return {
            "entry_id": entry_id,
            "set_name_candidate": "隐世回光",
        }

    original = mod.build_echo_set_recommend
    mod.build_echo_set_recommend = fake_build
    try:
        out = mod.build_echo_set_recommend_all(
            project_root=tmp_path,
            timeout=7.5,
            update_strategy=False,
            dry_run=True,
        )
    finally:
        mod.build_echo_set_recommend = original

    assert out["all"] is True
    assert out["total"] == 2
    assert out["success"] == 1
    assert out["failed"] == 1
    assert [x[0] for x in calls] == ["111", "222"]

    ok_item = [x for x in out["results"] if x.get("entry_id") == "111"][0]
    failed_item = [x for x in out["results"] if x.get("entry_id") == "222"][0]
    assert ok_item["character_name"] == "白芷"
    assert failed_item["character_name"] == "守岸人"
    assert "error" in failed_item
