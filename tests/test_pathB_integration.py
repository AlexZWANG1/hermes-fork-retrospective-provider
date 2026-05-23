"""集成测试 · 验证路 B 三处改动联动.

测试场景:
  1. RetrospectiveProvider 抽象 (改动 1) · 能正确创建子类
  2. MemoryManager.run_retrospective (改动 2) · 能调度 provider
  3. 主循环触发逻辑 (改动 3) · message_loader 注入正确
  4. Habit Reflector plugin · 整套链路跑通

注意: 这里我们没法跑真 Hermes (太大), 用 mock 验证接口契约和数据流.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path
from unittest.mock import MagicMock

# 把 fork 的 agent/ 加进 import path
FORK_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(FORK_ROOT))


def test_change1_retrospective_provider_subclass_works():
    """改动 1 · 能创建 RetrospectiveProvider 子类."""
    from agent.memory_provider import RetrospectiveProvider, Schedule, Claim, PromotionResult

    # 子类化测试
    class FakeProvider(RetrospectiveProvider):
        name = "fake"

        def is_available(self): return True
        def initialize(self, **kw): pass
        def system_prompt_block(self): return ""
        def prefetch(self, q, **kw): return ""
        def sync_turn(self, u, a, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, n, a, **kw): return ""
        def shutdown(self): pass

        def schedule(self):
            return Schedule(interval_hours=24, min_idle_hours=1.0)

        def analyze(self, messages, *, window_days=30):
            return [
                Claim(
                    claim="测试 claim",
                    classification="preference",
                    evidence=[
                        {"session_id": "s1", "turn_n": 1, "excerpt": "test"},
                        {"session_id": "s2", "turn_n": 2, "excerpt": "test"},
                        {"session_id": "s3", "turn_n": 3, "excerpt": "test"},
                    ],
                    confidence=0.9,
                    decay_at="2026-12-31",
                )
            ]

        def promote(self, claims):
            return PromotionResult(promoted_to_usermd=len(claims))

    p = FakeProvider()
    assert p.is_available()
    assert p.schedule().interval_hours == 24

    claims = p.analyze([], window_days=30)
    assert len(claims) == 1
    assert claims[0].confidence == 0.9

    result = p.promote(claims)
    assert result.promoted_to_usermd == 1


def test_change2_memory_manager_runs_retrospective():
    """改动 2 · MemoryManager.run_retrospective 能找到并调度 Provider."""
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import RetrospectiveProvider, Schedule, Claim, PromotionResult

    # Mock provider
    class TestProvider(RetrospectiveProvider):
        name = "test"
        analyze_called = False
        promote_called = False

        def is_available(self): return True
        def initialize(self, **kw): pass
        def system_prompt_block(self): return ""
        def prefetch(self, q, **kw): return ""
        def sync_turn(self, u, a, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, n, a, **kw): return ""
        def shutdown(self): pass

        def schedule(self): return Schedule(interval_hours=24, min_idle_hours=1.0)

        def analyze(self, messages, *, window_days=30):
            self.analyze_called = True
            assert len(messages) >= 10, "manager 应该过滤掉 <10 msg 的情况"
            return [Claim(
                claim="x", classification="preference",
                evidence=[{"session_id": f"s{i}", "turn_n": i, "excerpt": "y"} for i in range(3)],
                confidence=0.9, decay_at="2026-12-31",
            )]

        def promote(self, claims):
            self.promote_called = True
            return PromotionResult(promoted_to_usermd=len(claims))

    manager = MemoryManager()
    provider = TestProvider()
    manager.add_provider(provider)

    # ★ 改动 2 的核心 API
    result = manager.run_retrospective(
        trigger="test",
        message_loader=lambda window_days=30: [
            {"session_id": "s", "role": "user", "content": "hi", "turn_n": i, "timestamp": 0}
            for i in range(20)
        ],
    )

    assert provider.analyze_called, "analyze 没被调到"
    assert provider.promote_called, "promote 没被调到"
    assert len(result["ran"]) == 1
    assert len(result["errors"]) == 0


def test_change2_skips_when_few_messages():
    """改动 2 · 少于 10 条 message 时跳过."""
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import RetrospectiveProvider, Schedule, Claim, PromotionResult

    class TestProvider(RetrospectiveProvider):
        name = "test"
        analyze_called = False

        def is_available(self): return True
        def initialize(self, **kw): pass
        def system_prompt_block(self): return ""
        def prefetch(self, q, **kw): return ""
        def sync_turn(self, u, a, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, n, a, **kw): return ""
        def shutdown(self): pass

        def schedule(self): return Schedule()
        def analyze(self, messages, **kw):
            self.analyze_called = True
            return []
        def promote(self, claims): return PromotionResult()

    manager = MemoryManager()
    p = TestProvider()
    manager.add_provider(p)

    result = manager.run_retrospective(
        trigger="test",
        message_loader=lambda window_days=30: [{"session_id": "s"}],  # 只 1 条
    )

    assert not p.analyze_called, "<10 messages 时应该跳过 analyze"
    assert len(result["skipped"]) == 1
    assert "too few messages" in result["skipped"][0]["reason"]


def test_change2_handles_provider_errors_gracefully():
    """改动 2 · provider 抛错时 manager 不崩."""
    from agent.memory_manager import MemoryManager
    from agent.memory_provider import RetrospectiveProvider, Schedule, Claim, PromotionResult

    class BrokenProvider(RetrospectiveProvider):
        name = "broken"

        def is_available(self): return True
        def initialize(self, **kw): pass
        def system_prompt_block(self): return ""
        def prefetch(self, q, **kw): return ""
        def sync_turn(self, u, a, **kw): pass
        def get_tool_schemas(self): return []
        def handle_tool_call(self, n, a, **kw): return ""
        def shutdown(self): pass

        def schedule(self): return Schedule()
        def analyze(self, messages, **kw):
            raise RuntimeError("intentional test failure")
        def promote(self, claims): return PromotionResult()

    manager = MemoryManager()
    manager.add_provider(BrokenProvider())

    # 不应抛出, 应记录到 errors
    result = manager.run_retrospective(
        trigger="test",
        message_loader=lambda window_days=30: [{"session_id": "s"} for _ in range(20)],
    )

    assert len(result["errors"]) == 1
    assert result["errors"][0]["stage"] == "analyze"


def test_habit_reflector_plugin_end_to_end_dryrun():
    """整套链路 · habit_reflector plugin 跑 dry-run 跑通."""
    from agent.memory_manager import MemoryManager
    from plugins.memory.habit_reflector import HabitReflector

    manager = MemoryManager()
    reflector = HabitReflector()
    reflector._dry_run = True       # 用 canned response 不调真 API

    # Mock memory_manager 的 handle_tool_call (因为没真 memory_tool)
    write_calls = []
    def mock_handle_tool_call(name, args, **kw):
        write_calls.append({"name": name, "args": args})
        return json.dumps({"success": True})
    manager.handle_tool_call = mock_handle_tool_call

    manager.add_provider(reflector)

    # 模拟 initialize (manager 通常会调)
    import tempfile
    from datetime import datetime, timezone, timedelta
    with tempfile.TemporaryDirectory() as tmp:
        reflector.initialize("session_test_001",
                              hermes_home=tmp,
                              memory_manager=manager)

        # 跳过 cold-start guard: 把 installed_at 设到 8 天前
        state_file = Path(tmp) / "habit_memory" / ".state.json"
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        state_file.write_text(json.dumps({"installed_at": eight_days_ago}))

        # ★ 路 B 完整链路 · 主循环触发 → manager 调度 → reflector 分析 → 写 USER.md
        # force=True 跳过 allowed_hour_range 检查 (测试在白天跑也要能通过)
        result = manager.run_retrospective(
            trigger="conversation_start",
            force=True,
            message_loader=lambda window_days=30: _fake_messages(60),  # ≥50 才过阈值
        )

        assert len(result["errors"]) == 0, f"errors: {result['errors']}"
        assert len(result["ran"]) == 1
        run_info = result["ran"][0]
        assert run_info["name"] == "habit_reflector"
        # canned response 有 2 条 claims, 都满足 ≥0.85, 都应该 promote 成功
        assert run_info["result"].promoted_to_usermd >= 1
        # 应有至少 1 个 skill candidate (canned 里那条 habit)
        assert run_info["result"].skill_candidates >= 1

        # 验证 memory_manager.handle_tool_call 被调过 (≥1 次 memory_write)
        assert len(write_calls) >= 1
        assert any(c["name"] == "memory_write" for c in write_calls)
        assert any(c["args"]["target"] == "user" for c in write_calls)


def _fake_messages(n: int) -> list:
    """生成 n 条假 messages."""
    return [
        {
            "session_id": f"s_{i // 4:03d}",   # 每 4 条一个 session
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"测试内容 #{i}",
            "turn_n": (i % 4) + 1,
            "timestamp": 1748000000 + i * 60,
        }
        for i in range(n)
    ]


if __name__ == "__main__":
    """直接 python -m tests.test_pathB_integration 跑."""
    import traceback
    tests = [
        ("test_change1_retrospective_provider_subclass_works", test_change1_retrospective_provider_subclass_works),
        ("test_change2_memory_manager_runs_retrospective", test_change2_memory_manager_runs_retrospective),
        ("test_change2_skips_when_few_messages", test_change2_skips_when_few_messages),
        ("test_change2_handles_provider_errors_gracefully", test_change2_handles_provider_errors_gracefully),
        ("test_habit_reflector_plugin_end_to_end_dryrun", test_habit_reflector_plugin_end_to_end_dryrun),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}")
            traceback.print_exc()
            failed += 1
    print()
    print(f"=== {passed}/{passed+failed} passed ===")
    sys.exit(0 if failed == 0 else 1)
