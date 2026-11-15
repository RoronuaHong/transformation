"""subtitle_timing 的单元测试：规范化 + 硬字幕区间对齐。"""
from __future__ import annotations

import subtitle_timing as st


def test_normalize_overlap_min_max_dur():
    cues = [
        {"start": 0.0, "end": 2.0, "text": "a"},
        {"start": 1.0, "end": 3.0, "text": "b"},   # 与 a 重叠
        {"start": 3.0, "end": 3.2, "text": "c"},   # 过短
        {"start": 5.0, "end": 20.0, "text": "d"},  # 过长
    ]
    out = st.normalize_cue_timing(cues)
    assert len(out) == 4
    # 重叠收敛：b 起点推到 a 终点之后
    assert out[1]["start"] >= out[0]["end"] - 1e-6
    # 单调递增
    for i in range(1, len(out)):
        assert out[i]["start"] >= out[i - 1]["start"] - 1e-6
    # 最小时长（c 被延长到 ~0.8s）
    assert out[2]["end"] - out[2]["start"] >= 0.79
    # 最大时长（d 截断到 7s）
    assert out[3]["end"] - out[3]["start"] <= 7.01
    assert out[3]["start"] == 5.0


def test_normalize_negative_fixed():
    out = st.normalize_cue_timing([{"start": 1.0, "end": 0.5, "text": "x"}])
    assert out[0]["end"] > out[0]["start"]


def test_normalize_nested_cue_stays_monotonic():
    """整条嵌套在上一条内时,收敛重叠必须同时后移终点,否则产出倒挂/乱序 SRT。"""
    out = st.normalize_cue_timing([
        {"start": 0.0, "end": 6.0, "text": "a"},
        {"start": 3.0, "end": 4.0, "text": "b"},  # 完全嵌套在 a 内
        {"start": 5.0, "end": 5.8, "text": "c"},
    ])
    starts = [c["start"] for c in out]
    assert starts == sorted(starts)          # 时间单调递增
    assert all(c["end"] > c["start"] for c in out)  # 无倒挂
    # 收敛后 b 保住原时长 1.0s(只推起点会把它压成负时长)
    assert abs((out[1]["end"] - out[1]["start"]) - 1.0) < 0.02


def test_parse_srt_with_position_suffix():
    """带坐标后缀(X1:/Y1:)的 SRT 时间轴行不得让 parse_srt 崩溃(用枚举行下标)。"""
    from retime_from_hardsub import parse_srt

    srt = "1\n00:00:01,000 --> 00:00:02,000 X1:1 X2:2 Y1:3 Y2:4\nHello world\n"
    cues = parse_srt(srt)
    assert cues == [{"start": 1.0, "end": 2.0, "text": "Hello world"}]


def test_compare_coverage_detects_missing_hardsub_line():
    """核心门禁：硬字幕有、SRT 完全没有的漏句必须被抓出（align 抓不到这种）。"""
    cues = [
        {"text": "打死他", "start": 1.4, "end": 4.3},
        {"text": "绝无二心", "start": 36.3, "end": 37.2},
    ]
    intervals = [
        {"text": "打死他打死他", "start": 1.4, "end": 4.3},
        {"text": "不把我们当人看", "start": 5.5, "end": 6.4},   # 漏句
        {"text": "只顾自己升官发财", "start": 6.5, "end": 7.8}, # 漏句
        {"text": "绝无二心", "start": 36.3, "end": 37.2},
    ]
    rep = st.compare_coverage(cues, intervals)
    assert rep["ok"] is False
    assert len(rep["missing_in_srt"]) == 2
    texts = [m["text"] for m in rep["missing_in_srt"]]
    assert "不把我们当人看" in texts and "只顾自己升官发财" in texts
    assert rep["coverage"] < 1.0


def test_compare_coverage_covers_split_cues_and_reports_ok():
    """一条硬字幕被 ASR 切成两句时，拼接覆盖应算命中，不能误报缺句。"""
    cues = [
        {"text": "大人的武功", "start": 37.4},
        {"text": "威风八面", "start": 37.8},
    ]
    intervals = [{"text": "大人的武功威风八面", "start": 37.4, "end": 38.8}]
    rep = st.compare_coverage(cues, intervals)
    assert rep["missing_in_srt"] == []
    assert rep["ok"] is True


def test_compare_coverage_flags_drift():
    """文字对得上但起点偏了很多（没按原片显示时间出轴）要报漂移。"""
    cues = [{"text": "绝无二心", "start": 40.0}]
    intervals = [{"text": "绝无二心", "start": 36.3, "end": 37.2}]
    rep = st.compare_coverage(cues, intervals)
    assert rep["drift"] and abs(rep["drift"][0]["delta"] - 3.7) < 0.01


def test_compare_coverage_repeat_lines_no_false_drift():
    """重复句（同一句出现多次）必须各配时间最近的区间，不能全配第一条误报漂移。"""
    cues = [
        {"text": "打死他", "start": 1.4},
        {"text": "打死他", "start": 8.2},
        {"text": "打死他", "start": 13.4},
    ]
    intervals = [
        {"text": "打死他打死他", "start": 1.4, "end": 4.3},
        {"text": "打死他打死他", "start": 8.2, "end": 12.9},
        {"text": "打死他", "start": 13.4, "end": 16.6},
    ]
    rep = st.compare_coverage(cues, intervals)
    assert rep["drift"] == []          # 时间就近配对，无漂移
    assert rep["ok"] is True


def test_compare_coverage_minor_filler_not_blocking():
    """极短叹词（"啊"）缺失只记 minor_missing，不把门禁判失败。"""
    cues = [{"text": "天下无敌", "start": 44.7}]
    intervals = [
        {"text": "啊", "start": 44.7, "end": 45.1},
        {"text": "天下无敌", "start": 44.7, "end": 45.6},
    ]
    rep = st.compare_coverage(cues, intervals)
    assert rep["missing_in_srt"] == []
    assert [m["text"] for m in rep["minor_missing"]] == ["啊"]
    assert rep["ok"] is True


def test_compare_coverage_all_present_is_ok():
    cues = [{"text": "我不在意", "start": 3.7}, {"text": "还不仅仅如此", "start": 5.1}]
    intervals = [
        {"text": "我不在意", "start": 3.7, "end": 4.5},
        {"text": "还不仅仅如此", "start": 5.1, "end": 6.4},
    ]
    rep = st.compare_coverage(cues, intervals)
    assert rep["ok"] is True and rep["missing_in_srt"] == [] and rep["drift"] == []


def test_align_repeat_text_by_temporal_proximity():
    # 同一句"我不在意"在片中出现了两次，必须按时间就近匹配，而非有序指针漂移
    cues = [
        {"text": "我不在意", "start": 2.6},
        {"text": "我不在意", "start": 13.6},
    ]
    intervals = [
        {"text": "我不在意", "start": 15.9, "end": 16.8},  # 后一次
        {"text": "我不在意", "start": 3.7, "end": 4.5},    # 前一次
    ]
    intervals.sort(key=lambda x: x["start"])
    out = st.align_cues_to_intervals(cues, intervals)
    assert out[0][1] is intervals[0]   # cue@2.6 -> 3.7-4.5
    assert out[1][1] is intervals[1]   # cue@13.6 -> 15.9-16.8


def test_align_unmatched_none():
    cues = [{"text": "完全不同", "start": 1.0}]
    intervals = [{"text": "毫无关系", "start": 1.0, "end": 2.0}]
    out = st.align_cues_to_intervals(cues, intervals)
    assert out[0][1] is None


def test_apply_interval_timing():
    cues = [{"text": "a", "start": 0.0, "end": 9.0}]
    aligned = [(0, {"text": "a", "start": 1.3, "end": 4.3})]
    out = st.apply_interval_timing(cues, aligned)
    assert out[0]["start"] == 1.3 and out[0]["end"] == 4.3
    # 未命中保留原时间
    aligned2 = [(0, None)]
    out2 = st.apply_interval_timing(cues, aligned2)
    assert out2[0]["start"] == 0.0 and out2[0]["end"] == 9.0


def test_norm_strips_punct_for_similarity():
    a = st._norm("我，不在意！")
    b = st._norm("我不在意")
    assert a == b
    assert st._sim("我不在意。", "我不在意！") >= 0.99


def test_normalize_is_idempotent():
    """规范化必须幂等：源轨落盘前与翻译轨各调一次，结果须完全一致（锁轴）。"""
    cues = [
        {"start": 0.0, "end": 1.2, "text": "a"},
        {"start": 1.1, "end": 2.0, "text": "b"},   # 重叠
        {"start": 2.0, "end": 2.1, "text": "c"},   # 过短
        {"start": 3.0, "end": 12.0, "text": "d"},  # 过长
    ]
    once = st.normalize_cue_timing(cues)
    twice = st.normalize_cue_timing(once)
    assert once == twice


def test_apply_interval_timing_never_makes_cue_late():
    """命中区间的起点须严格等于原片显示起点（pad=0），不得前移更早。"""
    cues = [{"text": "x", "start": 9.0, "end": 20.0}]
    aligned = [(0, {"text": "x", "start": 12.43, "end": 16.67})]
    out = st.apply_interval_timing(cues, aligned)
    assert out[0]["start"] == 12.43 and out[0]["end"] == 16.67

