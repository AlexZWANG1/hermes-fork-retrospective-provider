"""Path B 改造集成测试 · 双层架构.

跑法:
  cd hermes-fork-pathB
  export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
  python3 tests/test_pathB_integration.py

不调真 API · 全部用 dry_run mode + 合成 fixture.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))


# ═════════════════════════════════════════════════════════════════════════════
# Test fixtures
# ═════════════════════════════════════════════════════════════════════════════

def _make_episode(session_id: str, turn_n: int, user_text: str, assistant_text: str = "ok"):
    from agent.memory_provider import Episode
    return Episode(
        episode_id=f"ep_{uuid.uuid4().hex[:8]}",
        session_id=session_id,
        turn_n=turn_n,
        timestamp=time.time() - (60 - turn_n) * 3600,
        user_content=user_text,
        assistant_content=assistant_text,
    )


def _make_60_episodes():
    episodes = []
    for i in range(60):
        sess = f"s_2026_05_{i // 8:02d}"
        if i % 4 == 0:
            text = "用表格列出 SWE-bench Pro 方法学对比"
        elif i % 5 == 0:
            text = "请给我 markdown table 格式"
        else:
            text = f"普通问题 {i}"
        episodes.append(_make_episode(sess, i % 8 + 1, text))
    return episodes


# ═════════════════════════════════════════════════════════════════════════════
# Test 1 · Layer A 抽象
# ═════════════════════════════════════════════════════════════════════════════

def test_episodic_provider_subclass_works():
    from agent.memory_provider import EpisodicProvider, Episode

    class FakeEpisodic(EpisodicProvider):
        @property
        def name(self): return "fake"
        def is_available(self): return True
        def get_tool_schemas(self): return []
        def handle_tool_call(self, *a, **k): return "{}"
        def initialize(self, *a, **k): self._store = []
        def record(self, ep): self._store.append(ep)
        def retrieve(self, query, *, top_k=5, session_id=None):
            return [e for e in self._store if query in e.user_content][:top_k]

    fake = FakeEpisodic()
    fake.initialize()
    fake.record(_make_episode("s1", 1, "用表格 abc"))
    fake.record(_make_episode("s1", 2, "无关消息"))
    results = fake.retrieve("表格")
    assert len(results) == 1
    assert "表格" in results[0].user_content
    print("  ✓ test_episodic_provider_subclass_works               [Layer A 抽象能用]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 2 · Layer B 抽象
# ═════════════════════════════════════════════════════════════════════════════

def test_actionable_habit_provider_subclass_works():
    from agent.memory_provider import (
        ActionableHabitProvider, HabitCandidate, FitnessReport, Schedule,
    )

    class FakeHabit(ActionableHabitProvider):
        @property
        def name(self): return "fake_habit"
        def is_available(self): return True
        @property
        def schedule(self): return Schedule()
        def get_tool_schemas(self): return []
        def handle_tool_call(self, *a, **k): return "{}"
        def initialize(self, *a, **k): self._promoted = []
        def propose_habit(self, episodes):
            return [HabitCandidate(
                candidate_id="hc_1", claim="use tables", classification="style",
                supporting_episodes=[e.episode_id for e in episodes[:3]],
                raw_confidence=0.9, proposed_at="2026-05-23",
            )]
        def fitness_test(self, c, qs):
            return FitnessReport(candidate_id=c.candidate_id, win_rate_B=0.8,
                                 verdict="pass", n_samples=len(qs))
        def await_user_approval(self, c): return True
        def promote(self, c): self._promoted.append(c.candidate_id)
        def revert(self, cid): self._promoted.remove(cid)

    h = FakeHabit()
    h.initialize()
    candidates = h.propose_habit(_make_60_episodes())
    assert len(candidates) == 1
    print("  ✓ test_actionable_habit_provider_subclass_works       [Layer B 抽象能用]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 3 · episodic_store plugin 全链路
# ═════════════════════════════════════════════════════════════════════════════

def test_episodic_store_plugin_record_retrieve():
    from plugins.memory.episodic_store import EpisodicStore

    with tempfile.TemporaryDirectory() as tmp:
        store = EpisodicStore()
        store.initialize(hermes_home=tmp)

        for i in range(5):
            store.record(_make_episode(
                "s_2026", i, f"用表格列 {i}" if i % 2 == 0 else f"无关 {i}",
            ))

        results = store.retrieve("表格", top_k=10)
        assert len(results) == 3, f"expected 3 hits, got {len(results)}"

        first_id = results[0].episode_id
        store.pin(first_id)
        results2 = store.retrieve("表格", top_k=10)
        assert results2[0].episode_id == first_id

    print("  ✓ test_episodic_store_plugin_record_retrieve          [Layer A 插件链路]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 4 · fitness gate 拒绝
# ═════════════════════════════════════════════════════════════════════════════

def test_habit_promoter_fitness_gate_rejects_weak_candidate():
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import HabitCandidate, FitnessReport
    from plugins.memory.habit_promoter import HabitPromoter

    write_log = []
    manager = MemoryManager()
    manager.handle_tool_call = lambda n, a, **kw: (write_log.append(a), "{}")[1]

    with tempfile.TemporaryDirectory() as tmp:
        promoter = HabitPromoter()
        manager.add_provider(promoter)
        promoter.initialize(
            hermes_home=tmp, memory_manager=manager, dry_run=True,
            promotion_require_user_approval=False,
        )

        weak = HabitCandidate(
            candidate_id="hc_weak", claim="weak rule", classification="style",
            supporting_episodes=["ep1"], raw_confidence=0.4,
            proposed_at="2026-05-23",
        )
        promoter.fitness_test = lambda c, qs: FitnessReport(
            candidate_id=c.candidate_id, win_rate_B=0.3,
            verdict="fail", n_samples=8,
        )

        report = manager.run_habit_promotion_pipeline(
            weak, provider=promoter, sample_queries=["q1", "q2"],
            require_user_approval=False,
        )

        assert report["decision"].startswith("rejected_fitness"), \
            f"expected rejection, got {report['decision']}"
        assert len(write_log) == 0

    print("  ✓ test_habit_promoter_fitness_gate_rejects_weak       [fitness 防漂移]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 5 · 人审 gate 阻挡 silent promote
# ═════════════════════════════════════════════════════════════════════════════

def test_habit_promoter_user_approval_gate_blocks_silent_promote():
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import HabitCandidate
    from plugins.memory.habit_promoter import HabitPromoter

    write_log = []
    manager = MemoryManager()
    manager.handle_tool_call = lambda n, a, **kw: (write_log.append(a), "{}")[1]

    with tempfile.TemporaryDirectory() as tmp:
        promoter = HabitPromoter()
        manager.add_provider(promoter)
        promoter.initialize(hermes_home=tmp, memory_manager=manager, dry_run=True)

        cand = HabitCandidate(
            candidate_id="hc_good", claim="use tables", classification="style",
            supporting_episodes=["ep1", "ep2"], raw_confidence=0.92,
            proposed_at="2026-05-23",
            intervention="use markdown table for comparisons",
        )

        promoter.await_user_approval = lambda c: False

        report = manager.run_habit_promotion_pipeline(
            cand, provider=promoter, sample_queries=["q1"] * 8,
            require_user_approval=True,
        )

        assert report["decision"] == "rejected_user_declined"
        assert len(write_log) == 0

    print("  ✓ test_habit_promoter_user_approval_gate_blocks       [人审防漂移]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 6 · 全链路绿灯
# ═════════════════════════════════════════════════════════════════════════════

def test_habit_promoter_full_pipeline_promotes_when_approved():
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import HabitCandidate
    from plugins.memory.habit_promoter import HabitPromoter

    write_log = []
    manager = MemoryManager()
    manager.handle_tool_call = lambda n, a, **kw: (write_log.append(a), "{}")[1]

    with tempfile.TemporaryDirectory() as tmp:
        promoter = HabitPromoter()
        manager.add_provider(promoter)
        promoter.initialize(hermes_home=tmp, memory_manager=manager, dry_run=True)

        cand = HabitCandidate(
            candidate_id="hc_full", claim="use tables", classification="style",
            supporting_episodes=["ep1", "ep2", "ep3"], raw_confidence=0.92,
            proposed_at="2026-05-23",
            intervention="use markdown table for comparisons",
        )
        promoter.await_user_approval = lambda c: True

        report = manager.run_habit_promotion_pipeline(
            cand, provider=promoter, sample_queries=["q1"] * 8,
            require_user_approval=True,
        )

        assert report["decision"] == "promoted", f"got {report['decision']}"
        assert len(write_log) == 1
        assert "table" in write_log[0]["content"].lower()

        audit = (Path(tmp) / "habit_memory" / "audit.log").read_text()
        assert "promoted" in audit
        assert "hc_full" in audit

    print("  ✓ test_habit_promoter_full_pipeline_promotes_when_ok  [全链路绿灯]")


# ═════════════════════════════════════════════════════════════════════════════
# Test 7 · propose_habit 蒸馏
# ═════════════════════════════════════════════════════════════════════════════

def test_propose_habit_distills_candidates():
    from plugins.memory.habit_promoter import HabitPromoter

    with tempfile.TemporaryDirectory() as tmp:
        promoter = HabitPromoter()
        promoter.initialize(hermes_home=tmp, dry_run=True)

        episodes = _make_60_episodes()
        candidates = promoter.propose_habit(episodes)

        assert len(candidates) == 1
        assert "结构化" in candidates[0].claim or "table" in candidates[0].claim.lower()
        assert candidates[0].raw_confidence > 0.8

    print("  ✓ test_propose_habit_distills_candidates              [蒸馏链路]")


# ═════════════════════════════════════════════════════════════════════════════
# Runner
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        test_episodic_provider_subclass_works,
        test_actionable_habit_provider_subclass_works,
        test_episodic_store_plugin_record_retrieve,
        test_habit_promoter_fitness_gate_rejects_weak_candidate,
        test_habit_promoter_user_approval_gate_blocks_silent_promote,
        test_habit_promoter_full_pipeline_promotes_when_approved,
        test_propose_habit_distills_candidates,
    ]
    print("\n=== Path B · 双层架构集成测试 ===\n")
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n=== {len(tests) - failed}/{len(tests)} passed ===\n")
    sys.exit(1 if failed else 0)
