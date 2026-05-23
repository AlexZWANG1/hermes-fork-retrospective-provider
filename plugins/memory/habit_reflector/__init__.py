"""Habit Reflector · 通过 RetrospectiveProvider 抽象接进 Hermes 内核.

这是路 B 的最终落点 · 把路 A (外挂版) 的业务逻辑通过 RetrospectiveProvider
接口注册成 Hermes 内核的一个 plugin · 享受官方调度.

═════════════════════════════════════════════════════════════════════════════
跟路 A 的关键差别:

  路 A (外挂):                       路 B (这个文件):
  · 文件 IO 读 state.db              · MemoryManager 喂 messages 进来
  · cron daemon 触发                 · MemoryManager 的 run_retrospective() 调
  · 直接写 USER.md 文件              · 通过 self.memory_manager 调 memory_write
  · Hermes 不知道你存在              · 注册成 Provider · Hermes 知道
  · 装在 ~/.hermes/hooks/            · 装在 hermes-agent 仓库内 plugins/memory/
═════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 从 Hermes 改动 1 加的新抽象 import
from agent.memory_provider import (
    RetrospectiveProvider,
    Schedule,
    Claim,
    PromotionResult,
)

logger = logging.getLogger(__name__)


# 路 A demo 里那 6 行 confidence 公式 · 路 B 里仍然用本地重算 (不信模型自评)
def compute_confidence(
    *, times_appeared: int, distinct_sessions: int,
    last_seen_within_days: int, user_explicit_confirmed: bool = False,
) -> float:
    """起步 0.10 + 3 信号 × 0.25 + 用户显式确认 +0.25 · 最高 1.0."""
    score = 0.10
    if times_appeared >= 5:
        score += 0.25
    if distinct_sessions >= 3:
        score += 0.25
    if last_seen_within_days <= 7:
        score += 0.25
    if user_explicit_confirmed:
        score += 0.25
    return min(score, 1.0)


# Default decay days per classification
DECAY_DAYS = {"preference": 90, "habit": 30, "task": 7, "constraint": 180}


class HabitReflector(RetrospectiveProvider):
    """读 30 天 messages → Haiku 蒸馏 → 把稳定 habit 写进 USER.md.

    这是路 A 外挂版的"内核版本" · 业务逻辑相同, 接入点完全不同.
    """

    name = "habit_reflector"

    def __init__(self):
        super().__init__()
        self._memory_manager = None    # 被 MemoryManager.add_provider() 注入
        self._hermes_home: Optional[Path] = None
        self._distiller_model = "claude-haiku-4-5"
        self._dry_run = False          # 测试用 · 跳过真 API 调用

    # ──────────────────────────────────────────────────────────────
    # MemoryProvider 基类必填的方法 (大部分对 retrospective 没意义 · 返回 noop)
    # ──────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """检查 Anthropic SDK 装了没."""
        try:
            import anthropic  # noqa
            return True
        except ImportError:
            if self._dry_run:
                return True       # 测试模式不要求真 SDK
            logger.warning("habit_reflector: anthropic SDK not installed; disabled")
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        """Manager 在启动时调 · 传 hermes_home 进来."""
        self._hermes_home = Path(kwargs.get("hermes_home", str(Path.home() / ".hermes")))
        self._memory_manager = kwargs.get("memory_manager")
        # 创建本插件需要的目录
        (self._hermes_home / "habit_memory").mkdir(parents=True, exist_ok=True)
        (self._hermes_home / "habit_memory" / "candidates").mkdir(exist_ok=True)

    def system_prompt_block(self) -> str:
        return ""    # retrospective provider 不直接注入 system prompt

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""    # 不参与当下 retrieval

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return       # 不参与 turn 同步

    def get_tool_schemas(self) -> list:
        return []    # 不暴露 tool

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        return ""

    def shutdown(self) -> None:
        return

    # ──────────────────────────────────────────────────────────────
    # ★ 路 B 改动 1 加的 RetrospectiveProvider 抽象方法 · 这里实现 ★
    # ──────────────────────────────────────────────────────────────

    def schedule(self) -> Schedule:
        """每天 1 次 · 凌晨 4 点 · idle ≥1h · 装上前 1 周不跑."""
        return Schedule(
            interval_hours=24,
            min_idle_hours=1.0,
            cold_start_weeks=1,
            allowed_hour_range=(2, 6),   # 凌晨 2-6 点窗口
        )

    def analyze(self, messages: List[Dict[str, Any]], *, window_days: int = 30) -> List[Claim]:
        """读 messages → 调 Haiku 蒸馏 → 解析 → 本地重算 confidence."""
        # cold-start guard
        state_file = self._hermes_home / "habit_memory" / ".state.json"
        if self._is_cold_start(state_file):
            logger.info("habit_reflector: in cold-start week 1, skipping analyze")
            return []
        if len(messages) < 50:
            logger.info("habit_reflector: too few messages (%d < 50)", len(messages))
            return []

        # 渲染 messages 给 prompt
        rendered = self._render_messages(messages)
        prompt = self._build_distill_prompt(rendered, window_days)

        # 调模型 (dry_run 用 canned response)
        if self._dry_run:
            raw = self._canned_response()
        else:
            raw = self._call_haiku(prompt)

        # 解析 + 本地重算 confidence
        claims = self._parse_claims(raw)
        logger.info("habit_reflector: analyzed %d messages → %d claims", len(messages), len(claims))
        return claims

    def promote(self, claims: List[Claim]) -> PromotionResult:
        """高 conf → 调 memory_manager 写 USER.md · 中 conf → 队列 · habit → skill candidate.

        ★ 关键差别于路 A: 我们不直接写文件, 而是通过 memory_manager 走官方 tool 接口.
        """
        result = PromotionResult()
        habit_dir = self._hermes_home / "habit_memory"

        # 先写日报 (这部分还是直接写文件 · 不影响 Hermes 主流程)
        today = datetime.now(timezone.utc).date().isoformat()
        report_path = habit_dir / f"{today}.md"
        report_path.write_text(self._render_daily_report(claims))

        for claim in claims:
            # habit 类 · 给 Curator 留候选 (路 A 也是这样)
            if claim.classification == "habit":
                self._write_skill_candidate(claim, habit_dir / "candidates")
                result.skill_candidates += 1

            # 分流写入
            if claim.confidence >= 0.85:
                if self._promote_to_usermd_via_manager(claim):
                    result.promoted_to_usermd += 1
            elif claim.confidence >= 0.70:
                self._queue_for_review(claim, habit_dir / "candidates")
                result.queued_for_review += 1
            else:
                result.dropped_low_conf += 1

        # 记录上次跑的时间
        state_file = habit_dir / ".state.json"
        self._save_state({"last_run_at": datetime.now(timezone.utc).isoformat()}, state_file)

        return result

    def on_promotion_complete(self, result: PromotionResult) -> None:
        logger.info(
            "habit_reflector: cycle done · %d promoted · %d queued · %d skill_candidates",
            result.promoted_to_usermd, result.queued_for_review, result.skill_candidates,
        )

    # ──────────────────────────────────────────────────────────────
    # 内部方法 (业务逻辑 · 跟路 A 几乎一样)
    # ──────────────────────────────────────────────────────────────

    def _promote_to_usermd_via_manager(self, claim: Claim) -> bool:
        """★ 这是路 B 跟路 A 最关键的差别 ★

        路 A: 直接 open file write USER.md (绕过 Hermes)
        路 B: 调 memory_manager.handle_tool_call("memory_write", ...) (走官方协议)

        好处: Hermes 知道这条 entry 是 reflector 写的 · 自动备份 · 自动 char budget 检查.
        """
        if self._memory_manager is None:
            logger.warning("habit_reflector: no memory_manager reference; cannot promote")
            return False

        entry = self._render_user_md_entry(claim)
        try:
            result_json = self._memory_manager.handle_tool_call(
                "memory_write",
                {"action": "add", "target": "user", "content": entry},
            )
            result = json.loads(result_json) if result_json else {"success": False}
            if result.get("success"):
                logger.info("habit_reflector: promoted %s → USER.md via manager", claim.classification)
                return True
            logger.warning("habit_reflector: memory_write returned error: %s", result.get("error"))
            return False
        except Exception as e:
            logger.error("habit_reflector: promote via manager failed: %s", e)
            return False

    def _render_user_md_entry(self, claim: Claim) -> str:
        sessions = len({e.get("session_id") for e in claim.evidence})
        return (
            f"{claim.claim}\n"
            f"  [conf {claim.confidence:.2f} · {sessions} sessions · "
            f"decay {claim.decay_at} · src reflector]"
        )

    def _render_messages(self, messages: List[Dict[str, Any]]) -> str:
        """跟路 A 的 render_messages_for_prompt 一样."""
        parts = []
        cur_session = None
        for m in messages:
            if m.get("session_id") != cur_session:
                cur_session = m["session_id"]
                parts.append(f"\n=== session {cur_session[:20]} ===")
            role_short = "U" if m["role"] == "user" else "A"
            content = m["content"].replace("\n", " ").strip()
            if len(content) > 300:
                content = content[:297] + "..."
            parts.append(f"[t{m.get('turn_n', '?')} {role_short}] {content}")
        return "\n".join(parts)[-80000:]   # 上限 80k 字符

    def _build_distill_prompt(self, rendered: str, window_days: int) -> str:
        return DISTILL_PROMPT_TEMPLATE.format(window_days=window_days, messages_text=rendered)

    def _call_haiku(self, prompt: str) -> dict:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError("anthropic SDK not installed")
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=self._distiller_model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        return json.loads(text.strip())

    def _canned_response(self) -> dict:
        """跟路 A demo 用同一份 canned response (复用测试 fixture)."""
        return {
            "claims": [
                {
                    "claim": "用户强偏好结构化输出（表格 + bullet）而非 prose",
                    "classification": "preference",
                    "evidence": [
                        {"session_id": "s_test_001", "turn_n": 1, "excerpt": "用表格"},
                        {"session_id": "s_test_002", "turn_n": 3, "excerpt": "改成 bullet"},
                        {"session_id": "s_test_003", "turn_n": 1, "excerpt": "表格列出"},
                    ],
                    "times_appeared": 14, "distinct_sessions": 8,
                    "last_seen_within_days": 2, "user_explicit_confirmed": True,
                    "intervention": "session 启动时优先使用表格 + bullet",
                },
                {
                    "claim": "高频任务：读 paper → 出表格 critique",
                    "classification": "habit",
                    "evidence": [
                        {"session_id": "s_test_010", "turn_n": 1, "excerpt": "看 paper"},
                        {"session_id": "s_test_011", "turn_n": 1, "excerpt": "读 METR"},
                        {"session_id": "s_test_012", "turn_n": 1, "excerpt": "读这篇论文"},
                    ],
                    "times_appeared": 6, "distinct_sessions": 5,
                    "last_seen_within_days": 3, "user_explicit_confirmed": False,
                    "intervention": "promote 为 skill: paper-summary-table",
                },
            ]
        }

    def _parse_claims(self, raw: dict) -> List[Claim]:
        claims = []
        for c in raw.get("claims", []):
            conf = compute_confidence(
                times_appeared=c.get("times_appeared", 0),
                distinct_sessions=c.get("distinct_sessions", 0),
                last_seen_within_days=c.get("last_seen_within_days", 999),
                user_explicit_confirmed=c.get("user_explicit_confirmed", False),
            )
            evidence = c.get("evidence", [])
            if len(evidence) < 3:
                continue
            classification = c.get("classification", "preference")
            decay_days = DECAY_DAYS.get(classification, 30)
            decay_at = (datetime.now(timezone.utc) + timedelta(days=decay_days)).date().isoformat()
            claims.append(Claim(
                claim=c["claim"],
                classification=classification,
                evidence=evidence,
                confidence=conf,
                decay_at=decay_at,
                intervention=c.get("intervention"),
            ))
        return claims

    def _write_skill_candidate(self, claim: Claim, candidates_dir: Path) -> None:
        candidates_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() else "-" for c in claim.claim.lower())[:60].strip("-")
        path = candidates_dir / f"{safe_name}.candidate"
        path.write_text(json.dumps({
            "claim": claim.claim,
            "intervention": claim.intervention,
            "confidence": claim.confidence,
            "source": "habit_reflector",
        }, ensure_ascii=False, indent=2))

    def _queue_for_review(self, claim: Claim, candidates_dir: Path) -> None:
        candidates_dir.mkdir(parents=True, exist_ok=True)
        path = candidates_dir / f"pending-{datetime.now().timestamp():.0f}.json"
        path.write_text(json.dumps(asdict(claim), ensure_ascii=False, indent=2))

    def _render_daily_report(self, claims: List[Claim]) -> str:
        head = (
            "---\n"
            f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"claims_total: {len(claims)}\n"
            f"source: habit_reflector via RetrospectiveProvider\n"
            "---\n\n"
        )
        if not claims:
            return head + "## No habits distilled\n"
        body = []
        for c in claims:
            ev_text = "\n".join(
                f"  - {e['session_id']}:t{e['turn_n']} \"{e['excerpt']}\""
                for e in c.evidence
            )
            body.append(
                f"## {c.classification} · {c.claim}\n"
                f"confidence: {c.confidence:.2f}\n"
                f"decay: {c.decay_at}\n"
                f"evidence:\n{ev_text}\n"
            )
        return head + "\n".join(body)

    def _is_cold_start(self, state_file: Path) -> bool:
        state = self._load_state(state_file)
        installed = state.get("installed_at")
        if installed is None:
            state["installed_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state(state, state_file)
            return True
        days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(installed)).days
        return days_since < 7   # 第 1 周不跑

    def _load_state(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_state(self, state: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


DISTILL_PROMPT_TEMPLATE = """You are a behavioral analyst examining a user's actual
conversation history with an AI agent (Hermes). Your job is to extract DURABLE
PATTERNS, not transient task state.

A pattern qualifies only if BOTH:
- (a) appears in ≥5 distinct turns across the window, AND
- (b) appears in ≥3 distinct sessions

Classifications: preference / habit / task / constraint.

For each pattern output:
- claim (one sentence)
- classification
- evidence: 3+ {{session_id, turn_n, excerpt}}
- times_appeared (int)
- distinct_sessions (int)
- last_seen_within_days (int)
- user_explicit_confirmed (bool)
- intervention (optional)

DO NOT output task state. DO NOT output claims with <3 evidence.
Output strict JSON: {{"claims": [...]}}.

Conversation window (last {window_days} days):
{messages_text}
"""
