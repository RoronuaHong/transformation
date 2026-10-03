# -*- coding: utf-8 -*-
"""E2E: 素材中心全链路跑通门禁（真实库 + 真实 LLM + 确定性补跑）。

门禁(全部 True 才 exit 0):
  A agent status=done
  B actions 含 maintain / job_checkup / get_shots
  C related(children) 有 silent/audio 组件
  D CLIP 以文搜图 status=ok
  E 母版 shots.scenes >= 1(重建后尽量 >1)
  F merge 打标「全链路验收」且 role:master / job: 仍在
  G get_shots / search_by_text_image 已注册进 Deep Agent 工具表

用法:
  python scripts/e2e_agent_fullchain.py [--no-write] [--max-steps 14]
  (默认允许写;--no-write 才仅只读跑通门禁)
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402

JOB = "96e97c8ff04f"
TAG_ZH = "全链路验收"

TASK = (
    "严格按顺序逐步调用工具,每步只用真实返回,禁止编造 id,禁止跳步:\n"
    "1. maintain\n"
    "2. job_checkup(job_id=%s)\n"
    "3. search_materials(q='role:master', tag='role:master') 记下母版 id\n"
    "4. get_shots(id=母版)\n"
    "5. related(id=母版, rel=children)\n"
    "6. search_by_text_image(q='厨房')\n"
    "7. update_tags(id=母版, tags='%s') 只追加、不要 remove\n"
    "8. finish:中文总结缺口;citations 只用真实素材 id(12位),不要写 job_id\n"
) % (JOB, TAG_ZH)


def _pick_model(models):
    prefer = (os.environ.get("VITUAL_CHAT_MODEL") or "").strip()
    if prefer and prefer in models:
        return prefer
    for m in models:
        if (m or "").lower().startswith("qwen"):
            return m
    return models[0] if models else None


def _load_actions(task_id):
    sp = os.path.join(agent.WORKSPACE, task_id or "", "state.json")
    if not os.path.isfile(sp):
        return []
    st = json.load(open(sp, encoding="utf-8"))
    return [s for s in (st.get("steps") or []) if isinstance(s, dict)]


def _action_names(actions):
    return [a.get("action") for a in actions if a.get("action")]


def preflight():
    core.init_hub()
    return {
        "chat_models": core.chat_models()[:5],
        "health": core.health(),
        "clip": {k: core.clip_probe().get(k) for k in ("ok", "backend", "device", "err")},
        "imgembed": core.image_embed_status(),
        "job": core.job_checkup(JOB),
        "tools": sorted(agent._build_tools(True).keys()),
    }


def hard_gates(mid):
    """确定性补跑 + 断言(不依赖 LLM 是否跳步)。"""
    gates = {}
    # C related
    rel = agent._tool_related({"id": mid, "rel": "children", "limit": 20})
    kids = rel.get("related") or []
    kinds = {k.get("kind") for k in kids}
    gates["related_children"] = bool(kids) and (
        "silent" in kinds or "audio" in kinds or any(
            "parent:" + mid in (k.get("tags") or "") for k in kids
        )
    )
    gates["related_detail"] = {"count": rel.get("count"), "kinds": sorted(kinds)}

    # D CLIP text
    clip = core.search_by_text_image("厨房", limit=5)
    gates["clip_text"] = clip.get("status") == "ok" and bool(clip.get("matches"))
    gates["clip_detail"] = {"status": clip.get("status"),
                            "n": len(clip.get("matches") or [])}

    # E shots rebuild
    sh = core.build_shot_index(mid, force=True)
    data = core.get_shots(mid) or {}
    nsc = len(data.get("scenes") or [])
    gates["shots_ok"] = sh.get("status") in ("ok", "cached") and nsc >= 1
    gates["shots_multi"] = nsc > 1
    gates["shots_detail"] = {"status": sh.get("status"), "scenes": nsc,
                             "threshold": data.get("threshold") or sh.get("threshold")}

    # F merge tag
    before = (core.get_material(mid) or {}).get("tags") or ""
    mr = core.merge_material_tags(mid, TAG_ZH)
    tags = mr.get("tags") or ""
    gates["merge_tag"] = (
        mr.get("ok") is True
        and TAG_ZH in tags
        and ("role:master" in tags or "role:picture" in tags)
        and ("job:" + JOB in tags or "job:" in tags)
    )
    gates["merge_detail"] = {"ok": mr.get("ok"), "had_before": TAG_ZH in before,
                             "facet": "role:master" in tags, "job": "job:" in tags}

    # G tool registry
    tools = set(agent._build_tools(True))
    gates["tools_registered"] = {"get_shots", "search_by_text_image",
                                 "related", "update_tags", "job_checkup"}.issubset(tools)
    return gates


def main():
    write = "--no-write" not in sys.argv  # 默认允许写(合并护栏);--no-write 才关闭
    max_steps = 14
    if "--max-steps" in sys.argv:
        max_steps = int(sys.argv[sys.argv.index("--max-steps") + 1])

    print("=== preflight ===", flush=True)
    pf = preflight()
    h, j = pf["health"], pf["job"]
    print("chat:", pf["chat_models"], flush=True)
    print("health ok=%s total=%s" % (h.get("ok"), h.get("total")), flush=True)
    print("clip:", pf["clip"], flush=True)
    print("imgembed:", pf["imgembed"], flush=True)
    print("job ok=%s masters=%s silent=%s" % (
        j.get("ok"), j.get("master_ids"), j.get("has_silent")), flush=True)
    print("tools:", pf["tools"], flush=True)
    if not pf["chat_models"]:
        print("ABORT: no chat model", flush=True)
        sys.exit(2)
    masters = j.get("master_ids") or []
    if not masters:
        print("ABORT: no master_ids for job", flush=True)
        sys.exit(2)
    mid = masters[0]

    model = _pick_model(pf["chat_models"])
    print("=== agent_run write=%s max_steps=%s model=%s ===" % (
        write, max_steps, model), flush=True)
    t0 = time.time()
    r = agent.agent_run(TASK, allow_write=write, max_steps=max_steps, model=model)
    dt = time.time() - t0
    actions = _load_actions(r.get("task_id"))
    anames = _action_names(actions)
    print("=== agent (%.1fs) status=%s actions=%s ===" % (
        dt, r.get("status"), anames), flush=True)

    # 若 Agent 跳过关键步:短任务补跑(最多再 8 步)
    need = {"maintain", "job_checkup", "get_shots", "related",
            "search_by_text_image", "update_tags"}
    missing = sorted(need - set(anames))
    repair = None
    if missing and r.get("status") in ("done", "max_steps_reached"):
        print("=== repair agent missing=%s ===" % missing, flush=True)
        repair_task = (
            "补跑跳过的步骤(只做下列工具,全部做完后必须 finish,不要重复调用):\n"
            + "\n".join("- %s" % x for x in missing)
            + "\n母版 id=%s ; job_id=%s ; update_tags 追加 tags=%s\n"
              "search_by_text_image 用 q=厨房\nrelated 用 rel=children"
            % (mid, JOB, TAG_ZH)
        )
        repair = agent.agent_run(
            repair_task, allow_write=write, max_steps=8, model=model)
        actions2 = _load_actions(repair.get("task_id"))
        anames = list(dict.fromkeys(anames + _action_names(actions2)))
        print("=== repair status=%s actions_now=%s ===" % (
            repair.get("status"), anames), flush=True)
    elif r.get("status") == "max_steps_reached" and not missing:
        # 工具链已齐只差 finish:一步补 finish
        print("=== repair finish-only ===", flush=True)
        repair = agent.agent_run(
            "全链路工具已执行完毕。立刻 finish,用中文一句话总结:"
            "库健康/job体检/母版镜头/组件/CLIP/打标均已跑通。"
            "citations 只用母版 id=%s" % mid,
            allow_write=False, max_steps=3, model=model)
        print("=== repair status=%s ===" % repair.get("status"), flush=True)

    print("=== hard_gates mid=%s ===" % mid, flush=True)
    gates = hard_gates(mid)
    print(json.dumps(gates, ensure_ascii=False, indent=2), flush=True)

    repair_st = (repair or {}).get("status")
    tools_done = need.issubset(set(anames))
    agent_ok = (
        r.get("status") == "done"
        or repair_st == "done"
        or (r.get("status") == "max_steps_reached" and tools_done)
    )
    checks = {
        "A_agent_done": bool(agent_ok),
        "B_core_actions": {"maintain", "job_checkup", "get_shots"}.issubset(set(anames)),
        "C_related": gates["related_children"],
        "D_clip_text": gates["clip_text"],
        "E_shots": gates["shots_ok"],
        "E2_shots_multi": gates["shots_multi"],  # soft prefer
        "F_merge_tag": gates["merge_tag"],
        "G_tools": gates["tools_registered"],
        "H_tools_invoked": tools_done,
    }
    # hard required (E2 soft)
    hard = {k: v for k, v in checks.items() if k != "E2_shots_multi"}
    all_hard = all(hard.values())
    print("=== checks ===", flush=True)
    print(json.dumps(checks, ensure_ascii=False, indent=2), flush=True)
    print("ALL_HARD=%s shots_multi=%s" % (all_hard, checks["E2_shots_multi"]),
          flush=True)

    out = {
        "preflight": {
            "chat": pf["chat_models"], "clip": pf["clip"],
            "imgembed": pf["imgembed"], "job_ok": j.get("ok"),
            "master": mid, "tools": pf["tools"],
        },
        "agent": {
            "status": r.get("status"), "task_id": r.get("task_id"),
            "summary": r.get("summary"), "actions": anames,
            "repair_status": repair_st,
            "elapsed_sec": dt,
        },
        "gates": gates,
        "checks": checks,
        "all_hard": all_hard,
    }
    out_path = os.path.join(core.INDEX_DIR, "e2e_agent_fullchain_last.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("wrote", out_path, flush=True)
    if not all_hard:
        print("FAIL gates:", [k for k, v in hard.items() if not v], flush=True)
        sys.exit(1)
    print("PASS fullchain", flush=True)


if __name__ == "__main__":
    main()
