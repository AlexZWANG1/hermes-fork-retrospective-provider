"""EpisodicStore · Layer A · 全量 episode 记录 + 检索 plugin.

设计原则 (回应 11.1 "学习不是万能"):
  - 这一层<b>不学习</b>. 不蒸馏 . 不修改 USER.md . 不注入 system prompt.
  - 只做一件事: 全量记录 user-agent turn + 关键字 / 时间 / pin 检索.
  - 价值: 即使 Layer B 的 habit 蒸馏给的是错的, 用户依然有<b>可靠的 episodic recall</b>.
  - 关于"信噪比": 我们的判断是 — 写入时不做价值判断, 价值判断推到 retrieve 时 (用 top_k + ranking).
    理由: 写入时判断 = 当下 LLM 觉得"无聊"的 turn 三个月后可能恰好是用户要找的那个.

配置 (plugin.yaml):
  enabled: true             # default-on
  storage_path: ~/.hermes/episodes/
  retention_days: 365       # 一年, 之后冷归档
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import EpisodicProvider, Episode

logger = logging.getLogger(__name__)


class EpisodicStore(EpisodicProvider):
    """JSONL-backed episodic store · 一个 session 一个文件 · O(1) append."""

    @property
    def name(self) -> str:
        return "episodic_store"

    def is_available(self) -> bool:
        return True

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [{
            "name": "recall_episode",
            "description": (
                "Search past user-agent turns by keyword / session. "
                "Returns up to top_k episodes with full content. "
                "Use when the user references something from a previous conversation."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword / phrase to search"},
                    "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
                    "session_id": {"type": "string", "description": "Optional · scope to one session"},
                },
                "required": ["query"],
            },
        }]

    def system_prompt_block(self) -> str:
        return (
            "You have access to a tool `recall_episode` that searches the user's "
            "full past conversation history. Use it when the user references "
            "something earlier ('like what I asked last week', 'remember when we...'). "
            "Do NOT use it speculatively on every turn — only when the user signals recall."
        )

    def initialize(self, session_id: str = "", *, hermes_home: str = "", **kwargs) -> None:
        self._home = Path(hermes_home).expanduser()
        self._episodes_dir = self._home / "episodes"
        self._episodes_dir.mkdir(parents=True, exist_ok=True)
        self._pins_file = self._home / "episodes" / ".pins.json"
        self._session_id = session_id
        logger.info("episodic_store initialized · home=%s", self._episodes_dir)

    # ── EpisodicProvider 实现 ───────────────────────────────────────────────

    def record(self, episode: Episode) -> None:
        """Append 一行 JSON 到对应 session 文件 · 永不阻塞主循环."""
        if not episode.episode_id:
            episode.episode_id = f"ep_{uuid.uuid4().hex[:12]}"
        if not episode.timestamp:
            episode.timestamp = time.time()

        session_file = self._episodes_dir / f"{episode.session_id}.jsonl"
        try:
            with session_file.open("a") as f:
                f.write(json.dumps({
                    "episode_id": episode.episode_id,
                    "session_id": episode.session_id,
                    "turn_n": episode.turn_n,
                    "timestamp": episode.timestamp,
                    "user_content": episode.user_content,
                    "assistant_content": episode.assistant_content,
                    "context_refs": episode.context_refs,
                    "outcome": episode.outcome,
                    "tags": episode.tags,
                    "user_pinned": episode.user_pinned,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("episodic_store.record failed: %s", e)

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        session_id: Optional[str] = None,
    ) -> List[Episode]:
        """简单关键字 + 时间排序检索 · 不做语义 embedding (信号 keep simple).

        信噪比策略:
          - 关键字命中数 → 主排序
          - user_pinned=True → 强加分
          - 越近的越优先 → 时间 tiebreak
        """
        if not query:
            return []
        q_tokens = [t.lower() for t in query.split() if t.strip()]
        scored: List[tuple[float, Episode]] = []

        files = (
            [self._episodes_dir / f"{session_id}.jsonl"]
            if session_id else
            sorted(self._episodes_dir.glob("*.jsonl"))
        )

        for f in files:
            if not f.exists():
                continue
            try:
                for line in f.read_text().splitlines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    text = (d.get("user_content", "") + " " + d.get("assistant_content", "")).lower()
                    hits = sum(1 for tok in q_tokens if tok in text)
                    if hits == 0:
                        continue
                    score = hits * 10.0
                    if d.get("user_pinned"):
                        score += 50.0
                    score += d.get("timestamp", 0) / 1e10  # 越新越优
                    scored.append((score, Episode(**d)))
            except Exception as e:
                logger.warning("episodic_store.retrieve · file %s failed: %s", f, e)
                continue

        scored.sort(key=lambda x: -x[0])
        return [ep for _, ep in scored[:top_k]]

    def pin(self, episode_id: str) -> None:
        pins = self._load_pins()
        pins[episode_id] = True
        self._save_pins(pins)

    def unpin(self, episode_id: str) -> None:
        pins = self._load_pins()
        pins.pop(episode_id, None)
        self._save_pins(pins)

    def _load_pins(self) -> Dict[str, bool]:
        if self._pins_file.exists():
            try:
                return json.loads(self._pins_file.read_text())
            except Exception:
                return {}
        return {}

    def _save_pins(self, pins: Dict[str, bool]) -> None:
        self._pins_file.write_text(json.dumps(pins, ensure_ascii=False, indent=2))

    # ── MemoryProvider 标准方法 ─────────────────────────────────────────────

    def handle_tool_call(self, tool_name: str, tool_input: Dict[str, Any], **kwargs) -> str:
        if tool_name != "recall_episode":
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        query = tool_input.get("query", "")
        top_k = int(tool_input.get("top_k", 5))
        session_id = tool_input.get("session_id")
        episodes = self.retrieve(query, top_k=top_k, session_id=session_id)
        return json.dumps({
            "success": True,
            "count": len(episodes),
            "episodes": [
                {
                    "episode_id": e.episode_id,
                    "session_id": e.session_id,
                    "turn_n": e.turn_n,
                    "timestamp": e.timestamp,
                    "user_content": e.user_content[:200],
                    "assistant_content": e.assistant_content[:200],
                    "user_pinned": e.user_pinned,
                }
                for e in episodes
            ],
        }, ensure_ascii=False)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """关键设计: 不主动 prefetch 注入 system prompt.
        Layer A 的检索由 agent 通过 recall_episode tool 显式发起."""
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """每 turn agent 通过 record() 调一次, sync_turn 这里 noop."""
        return None
