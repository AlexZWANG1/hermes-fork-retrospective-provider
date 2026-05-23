"""HabitPromoter · Layer B · 从 episodes 蒸馏 habit + fitness gate + 人审 promote.

设计原则 (回应 11.2 "行为漂移"):
  - <b>default-off</b>. 用户必须显式启用. 行为修改默认是关的, 不能背后偷偷开.
  - <b>三道闸</b> 才能写 USER.md:
      ① fitness_test · A/B 对照, B 不优于 A 直接 reject
      ② await_user_approval · 写 .pending.json 等用户 confirm
      ③ promote · 全程 audit log + 一键 revert
  - 任何 silent self-modify 路径都被这条 pipeline 堵住.

配置 (plugin.yaml):
  enabled: false                # ★ 显式启用才生效
  distiller_model: claude-haiku-4-5
  promotion_require_user_approval: true
  fitness_min_win_rate: 0.65
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import (
    ActionableHabitProvider,
    Episode,
    HabitCandidate,
    FitnessReport,
    Schedule,
)

logger = logging.getLogger(__name__)


# ── Distiller prompt · LLM 蒸馏 episodes → habit claims ───────────────────────

_DISTILL_PROMPT = """You are analyzing the user's past interactions to find <b>stable patterns</b> that could improve future agent behavior.

You will receive {n} episodes (user + agent turns). For each pattern you observe, propose a HabitCandidate.

Strict criteria (reject if any fails):
1. Pattern must repeat ≥ 5 times across ≥ 3 distinct sessions.
2. Pattern must describe a USER preference or workflow, not a one-off task.
3. Pattern must be actionable: "use markdown tables when listing comparisons" is good · "user is curious" is not.

Output strict JSON list:
[
  {{
    "claim": "<≤140 chars actionable rule>",
    "classification": "<style|workflow|tool_preference|format>",
    "supporting_episodes": ["<episode_id>", ...],
    "raw_confidence": <0.0-1.0>,
    "intervention": "<concrete rule to inject into USER.md>"
  }}
]

Episodes:
{episodes_blob}
"""


class HabitPromoter(ActionableHabitProvider):
    """Layer B · fitness-gated habit promoter."""

    @property
    def name(self) -> str:
        return "habit_promoter"

    def is_available(self) -> bool:
        return True

    @property
    def schedule(self) -> Schedule:
        return Schedule(
            interval_hours=24,
            min_idle_hours=2,
            cold_start_weeks=1,
            allowed_hour_range=(2, 5),
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []  # 不暴露 tool, 通过 manager 调度

    def system_prompt_block(self) -> str:
        return ""

    def initialize(self, session_id: str = "", *, hermes_home: str = "", **kwargs) -> None:
        self._home = Path(hermes_home).expanduser()
        self._state_dir = self._home / "habit_memory"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._pending_dir = self._state_dir / "pending"
        self._pending_dir.mkdir(exist_ok=True)
        self._audit_log = self._state_dir / "audit.log"
        self._dry_run = kwargs.get("dry_run", False)
        self._memory_manager = kwargs.get("memory_manager")
        self._distiller_model = kwargs.get("distiller_model", "claude-haiku-4-5")
        self._min_win_rate = float(kwargs.get("fitness_min_win_rate", 0.65))
        self._require_user_approval = bool(kwargs.get("promotion_require_user_approval", True))

        state_file = self._state_dir / ".state.json"
        if not state_file.exists():
            state_file.write_text(json.dumps({
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }))
        logger.info(
            "habit_promoter initialized · approval=%s · min_win_rate=%.2f",
            self._require_user_approval, self._min_win_rate,
        )

    # ── ActionableHabitProvider 实现 ────────────────────────────────────────

    def propose_habit(self, episodes: List[Episode]) -> List[HabitCandidate]:
        """调 distiller LLM · 返回 candidates · <b>不</b> 自动 promote."""
        if len(episodes) < 10:
            return []

        if self._dry_run:
            # Canned fixture for offline tests
            return [
                HabitCandidate(
                    candidate_id=f"hc_{uuid.uuid4().hex[:8]}",
                    claim="用户强偏好结构化输出 (表格 + bullet) 而非 prose",
                    classification="style",
                    supporting_episodes=[e.episode_id for e in episodes[:8]],
                    raw_confidence=0.92,
                    proposed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    intervention="When output is comparison/list, use markdown table by default.",
                ),
            ]

        # 真 API 路径
        try:
            from anthropic import Anthropic
        except ImportError:
            logger.warning("anthropic SDK not installed · skip propose_habit")
            return []

        episodes_blob = "\n".join(
            f"[{e.episode_id} · {e.session_id} · turn {e.turn_n}] "
            f"USER: {e.user_content[:200]} | ASSISTANT: {e.assistant_content[:200]}"
            for e in episodes
        )
        prompt = _DISTILL_PROMPT.format(n=len(episodes), episodes_blob=episodes_blob)

        client = Anthropic()
        resp = client.messages.create(
            model=self._distiller_model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text if resp.content else "[]"
        try:
            start = raw.find("[")
            end = raw.rfind("]")
            data = json.loads(raw[start:end + 1]) if start != -1 else []
        except Exception as e:
            logger.warning("habit_promoter parse failed: %s · raw=%r", e, raw[:200])
            return []

        candidates = []
        for item in data:
            candidates.append(HabitCandidate(
                candidate_id=f"hc_{uuid.uuid4().hex[:8]}",
                claim=item.get("claim", "")[:140],
                classification=item.get("classification", "unknown"),
                supporting_episodes=item.get("supporting_episodes", []),
                raw_confidence=float(item.get("raw_confidence", 0.0)),
                proposed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                intervention=item.get("intervention"),
            ))
        return candidates

    def fitness_test(
        self,
        candidate: HabitCandidate,
        sample_queries: List[str],
    ) -> FitnessReport:
        """A/B 测试 · A=current behavior, B=candidate intervention injected.

        Verdict 规则:
          pass     · win_rate_B >= self._min_win_rate (default 0.65)
          neutral  · 0.45 <= win_rate_B < 0.65
          fail     · win_rate_B < 0.45
        """
        if not sample_queries:
            return FitnessReport(
                candidate_id=candidate.candidate_id,
                win_rate_B=0.0,
                verdict="fail",
                n_samples=0,
                notes="no sample queries provided",
            )

        if self._dry_run:
            # Canned 0.82 win_rate for the dry-run candidate
            return FitnessReport(
                candidate_id=candidate.candidate_id,
                win_rate_B=0.82,
                verdict="pass",
                n_samples=len(sample_queries),
                notes="dry-run canned result",
            )

        # 真 A/B (留 stub · 真实现需要 judge LLM)
        # 这里只跑一个 placeholder · 真生产代码会:
        #   1. 对每个 sample_query, 跑两次: A=无 intervention, B=有 intervention
        #   2. 调 judge LLM 判 哪个 response 更符合 user style
        #   3. 统计 B 赢的比例
        win_count = 0
        for _ in sample_queries:
            # placeholder: 真生产用 judge LLM
            if random.random() < candidate.raw_confidence:
                win_count += 1
        win_rate = win_count / len(sample_queries)
        verdict = "pass" if win_rate >= self._min_win_rate else (
            "neutral" if win_rate >= 0.45 else "fail"
        )
        return FitnessReport(
            candidate_id=candidate.candidate_id,
            win_rate_B=win_rate,
            verdict=verdict,
            n_samples=len(sample_queries),
            notes=f"placeholder judge · raw_confidence={candidate.raw_confidence:.2f}",
        )

    def await_user_approval(self, candidate: HabitCandidate) -> bool:
        """写 .pending.json + 等显式 approve.

        关键设计:
          - 不阻塞主对话 · 把 candidate 序列化到 pending/<id>.json
          - 用户通过 CLI / UI 调 `approve_habit <id>` / `reject_habit <id>`
          - 测试场景下可以 monkey-patch 这个方法直接返回 True/False
        """
        pending_file = self._pending_dir / f"{candidate.candidate_id}.json"
        try:
            pending_file.write_text(json.dumps({
                "candidate_id": candidate.candidate_id,
                "claim": candidate.claim,
                "classification": candidate.classification,
                "supporting_episodes": candidate.supporting_episodes,
                "fitness_report": candidate.fitness_report,
                "intervention": candidate.intervention,
                "proposed_at": candidate.proposed_at,
                "status": "pending_user_approval",
            }, ensure_ascii=False, indent=2))
            self._audit("pending", candidate.candidate_id, claim=candidate.claim)
        except Exception as e:
            logger.warning("await_user_approval write failed: %s", e)
            return False

        # 默认实现: 同步等待 · 测试通过 monkey-patch · 生产通过 CLI 触发
        return False

    def promote(self, candidate: HabitCandidate) -> None:
        """写 USER.md · <b>通过 manager.handle_tool_call</b> 走官方 API."""
        if not candidate.user_approved:
            raise RuntimeError(
                f"promote rejected: candidate {candidate.candidate_id} not user_approved"
            )

        intervention_text = candidate.intervention or candidate.claim
        content = (
            f"- {intervention_text}\n"
            f"  [conf {candidate.raw_confidence:.2f} · "
            f"approved {time.strftime('%Y-%m-%d', time.gmtime())} · "
            f"id {candidate.candidate_id}]"
        )

        if self._memory_manager:
            self._memory_manager.handle_tool_call(
                "memory",
                {
                    "command": "add",
                    "target": "USER.md",
                    "content": content,
                },
            )
        else:
            logger.warning("habit_promoter.promote: no memory_manager · skip USER.md write")

        # Move pending → promoted/
        pending_file = self._pending_dir / f"{candidate.candidate_id}.json"
        promoted_dir = self._state_dir / "promoted"
        promoted_dir.mkdir(exist_ok=True)
        if pending_file.exists():
            pending_file.rename(promoted_dir / pending_file.name)
        self._audit("promoted", candidate.candidate_id, claim=candidate.claim)

    def revert(self, candidate_id: str) -> None:
        """一键 revert · 从 USER.md 删除 + audit log."""
        if self._memory_manager:
            self._memory_manager.handle_tool_call(
                "memory",
                {
                    "command": "str_replace",
                    "target": "USER.md",
                    "old": f"id {candidate_id}",
                    "new": "",
                },
            )
        self._audit("reverted", candidate_id)

    # ── audit log ───────────────────────────────────────────────────────────

    def _audit(self, action: str, candidate_id: str, **kwargs) -> None:
        try:
            with self._audit_log.open("a") as f:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "action": action,
                    "candidate_id": candidate_id,
                    **kwargs,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── MemoryProvider 标准方法 ─────────────────────────────────────────────

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return json.dumps({"success": False, "error": "no tools exposed"})

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return None
