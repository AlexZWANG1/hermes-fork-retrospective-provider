"""Abstract base class for pluggable memory providers.

Memory providers give the agent persistent recall across sessions.
The MemoryManager enforces a one-external-provider limit to prevent
tool schema bloat and conflicting memory backends.

External providers (Honcho, Hindsight, Mem0, etc.) are registered
and managed via MemoryManager. Only one external provider runs at a
time.

Registration:
  Plugins ship in plugins/memory/<name>/ and are activated via
  the memory.provider config key.

Lifecycle (called by MemoryManager, wired in run_agent.py):
  initialize()          — connect, create resources, warm up
  system_prompt_block()  — static text for the system prompt
  prefetch(query)        — background recall before each turn
  sync_turn(user, asst)  — async write after each turn
  get_tool_schemas()     — tool schemas to expose to the model
  handle_tool_call()     — dispatch a tool call
  shutdown()             — clean exit

Optional hooks (override to opt in):
  on_turn_start(turn, message, **kwargs) — per-turn tick with runtime context
  on_session_end(messages)               — end-of-session extraction
  on_session_switch(new_session_id, **kwargs) — mid-process session_id rotation
  on_pre_compress(messages) -> str       — extract before context compression
  on_memory_write(action, target, content, metadata=None) — mirror built-in memory writes
  on_delegation(task, result, **kwargs)  — parent-side observation of subagent work
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'honcho', 'hindsight')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured, has credentials, and is ready.

        Called during agent init to decide whether to activate the provider.
        Should not make network calls — just check config and installed deps.
        """

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session.

        Called once at agent startup. May create resources (banks, tables),
        establish connections, start background threads, etc.

        kwargs always include:
          - hermes_home (str): The active HERMES_HOME directory path. Use this
            for profile-scoped storage instead of hardcoding ``~/.hermes``.
          - platform (str): "cli", "telegram", "discord", "cron", etc.

        kwargs may also include:
          - agent_context (str): "primary", "subagent", "cron", or "flush".
            Providers should skip writes for non-primary contexts (cron system
            prompts would corrupt user representations).
          - agent_identity (str): Profile name (e.g. "coder"). Use for
            per-profile provider identity scoping.
          - agent_workspace (str): Shared workspace name (e.g. "hermes").
          - parent_session_id (str): For subagents, the parent's session_id.
          - user_id (str): Platform user identifier (gateway sessions).
        """

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt.

        Called during system prompt assembly. Return empty string to skip.
        This is for STATIC provider info (instructions, status). Prefetched
        recall context is injected separately via prefetch().
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Return formatted text to inject as
        context, or empty string if nothing relevant. Implementations
        should be fast — use background threads for the actual recall
        and return cached results here.

        session_id is provided for providers serving concurrent sessions
        (gateway group chats, cached agents). Providers that don't need
        per-session scoping can ignore it.
        """
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. The result will be consumed
        by prefetch() on the next turn. Default is no-op — providers
        that do background prefetching should override this.
        """

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Persist a completed turn to the backend.

        Called after each turn. Should be non-blocking — queue for
        background processing if the backend has latency.
        """

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this provider exposes.

        Each schema follows the OpenAI function calling format:
        {"name": "...", "description": "...", "parameters": {...}}

        Return empty list if this provider has no tools (context-only).
        """

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle a tool call for one of this provider's tools.

        Must return a JSON string (the tool result).
        Only called for tool names returned by get_tool_schemas().
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks (override to opt in) ---------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message.

        Use for turn-counting, scope management, periodic maintenance.

        kwargs may include: remaining_tokens, model, platform, tool_count.
        Providers use what they need; extras are ignored.
        """

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Use for end-of-session fact extraction, summarization, etc.
        messages is the full conversation history.

        NOT called after every turn — only at actual session boundaries
        (CLI exit, /reset, gateway session expiry).
        """

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        """Called when the agent switches session_id mid-process.

        Fires on ``/resume``, ``/branch``, ``/reset``, ``/new`` (CLI), the
        gateway equivalents, and context compression — any path that
        reassigns ``AIAgent.session_id`` without tearing the provider down.

        Providers that cache per-session state in ``initialize()``
        (``_session_id``, ``_document_id``, accumulated turn buffers,
        counters) should update or reset that state here so subsequent
        writes land in the correct session's record.

        Parameters
        ----------
        new_session_id:
            The session_id the agent just switched to.
        parent_session_id:
            The previous session_id, if meaningful — set for ``/branch``
            (fork lineage), context compression (continuation lineage),
            and ``/resume`` (the session we're leaving). Empty string
            when no lineage applies.
        reset:
            ``True`` when this is a genuinely new conversation, not a
            resumption of an existing one. Fired by ``/reset`` / ``/new``.
            Providers should flush accumulated per-session buffers
            (``_session_turns``, ``_turn_counter``, etc.) when this is
            set. ``False`` for ``/resume`` / ``/branch`` / compression
            where the logical conversation continues under the new id.

        Default is no-op for backward compatibility.
        """

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Use to extract insights from messages about to be compressed.
        messages is the list that will be summarized/discarded.

        Return text to include in the compression summary prompt so the
        compressor preserves provider-extracted insights. Return empty
        string for no contribution (backwards-compatible default).
        """
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        """Called on the PARENT agent when a subagent completes.

        The parent's memory provider gets the task+result pair as an
        observation of what was delegated and what came back. The subagent
        itself has no provider session (skip_memory=True).

        task: the delegation prompt
        result: the subagent's final response
        child_session_id: the subagent's session_id
        """

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return config fields this provider needs for setup.

        Used by 'hermes memory setup' to walk the user through configuration.
        Each field is a dict with:
          key:         config key name (e.g. 'api_key', 'mode')
          description: human-readable description
          secret:      True if this should go to .env (default: False)
          required:    True if required (default: False)
          default:     default value (optional)
          choices:     list of valid values (optional)
          url:         URL where user can get this credential (optional)
          env_var:     explicit env var name for secrets (default: auto-generated)

        Return empty list if no config needed (e.g. local-only providers).
        """
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Write non-secret config to the provider's native location.

        Called by 'hermes memory setup' after collecting user inputs.
        ``values`` contains only non-secret fields (secrets go to .env).
        ``hermes_home`` is the active HERMES_HOME directory path.

        Providers with native config files (JSON, YAML) should override
        this to write to their expected location. Providers that use only
        env vars can leave the default (no-op).

        All new memory provider plugins MUST implement either:
        - save_config() for native config file formats, OR
        - use only env vars (in which case get_config_schema() fields
          should all have ``env_var`` set and this method stays no-op).
        """

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry.

        action: 'add', 'replace', or 'remove'
        target: 'memory' or 'user'
        content: the entry content
        metadata: structured provenance for the write, when available. Common
          keys include ``write_origin``, ``execution_context``, ``session_id``,
          ``parent_session_id``, ``platform``, and ``tool_name``.

        Use to mirror built-in memory writes to your backend.
        """


# ═════════════════════════════════════════════════════════════════════════════
# ★ 路 B 改动 1 · 加 RetrospectiveProvider 抽象（以下全部新增 · ~70 行）
#
# 大白话讲这一节加了什么:
# ─────────────────────────────────────
# 原来 Hermes 的 MemoryProvider 是给"当下记忆插件"用的——agent 当前 turn 发生啥
# 就立刻 sync_turn / prefetch 一下. 没有"事后回顾"这个概念.
#
# 我们要加的"回顾型记忆插件"跟它不一样:
#   · 它不是 turn 级别的, 而是 cron / idle 触发的批处理
#   · 它要看一段历史 messages, 而不只当下 turn
#   · 它的输出是 "claims" (一句话结论 + 证据), 不是注入 prompt 的字符串
#   · 它有自己的 schedule (什么时候该跑)
#
# 所以我们在 Hermes 现有 MemoryProvider 体系里加一个<b>子类</b>叫
# RetrospectiveProvider, 把上面这些"事后回顾"特有的能力规范出来.
#
# 任何想做 "agent 回头看过去 N 天发现 user habit" 这件事的人, 都按这个接口实现.
# Hermes 内核负责调度它们 (见 改动 2 的 MemoryManager.run_retrospective).
# ═════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field


@dataclass
class Schedule:
    """一个 Provider 什么时候该跑的描述."""

    # 间隔小时数. 比如 24 = 每天跑一次.
    interval_hours: int = 24

    # 用户必须 idle 多久才能跑 (避免打扰交互).
    min_idle_hours: float = 1.0

    # 冷启动周数 · 装上后前 N 周不跑 (攒数据).
    cold_start_weeks: int = 1

    # 在 idle 触发时强制要求的小时区间 (None = 任何时段都行).
    # 例如 (2, 6) = 只允许凌晨 2-6 点跑.
    allowed_hour_range: Optional[tuple] = None


@dataclass
class Claim:
    """Retrospective 分析产出的一个'结论' · 带证据 · 带置信度 · 带过期."""

    claim: str                              # 一句话结论
    classification: str                     # "preference" | "habit" | "task" | "constraint"
    evidence: List[Dict[str, Any]]          # 至少 3 个 session_id/turn_n/excerpt 引用
    confidence: float                       # 0.0 - 1.0
    decay_at: str                           # ISO date, 何时过期
    intervention: Optional[str] = None      # 给 agent 的可选指令
    source: str = "retrospective"


@dataclass
class PromotionResult:
    """promote() 跑完后告诉调度器发生了什么."""

    promoted_to_usermd: int = 0       # ≥ 0.85 conf 自动写入 USER.md 的数量
    queued_for_review: int = 0        # 0.7-0.85 入待审 queue
    dropped_low_conf: int = 0         # < 0.7 仅留日报
    skill_candidates: int = 0         # habit 类标记给 Curator 的


class RetrospectiveProvider(MemoryProvider):
    """事后回顾型记忆插件 · 基类.

    实现这个类的插件由 MemoryManager 在 idle / cron 时机主动调度,
    而不是每 turn 都跑. 它们看一段历史 messages 蒸馏出 stable claims,
    自动 promote 到 USER.md 或队列等用户审.

    跟现有的 MemoryProvider 区别:
      · MemoryProvider: agent 当下需要时拉 (prefetch / sync_turn)
      · RetrospectiveProvider: Hermes 定时主动调 (analyze + promote)
    """

    @abstractmethod
    def schedule(self) -> Schedule:
        """这个 provider 多久跑一次 / 什么时候允许跑."""

    @abstractmethod
    def analyze(self, messages: List[Dict[str, Any]], *, window_days: int = 30) -> List[Claim]:
        """看一段 messages 历史, 蒸馏出一组 Claim.

        参数 messages 是 [{session_id, role, content, turn_n, timestamp}, ...] 列表,
        由 MemoryManager 从 state.db 读出来喂进来.

        实现侧应当: 调小模型 (Haiku 等) 蒸馏 + 本地公式重算 confidence + 应用 blacklist.
        """

    @abstractmethod
    def promote(self, claims: List[Claim]) -> PromotionResult:
        """决定 claims 怎么处理 · 写 USER.md / 入待审 / drop / 标 skill 候选.

        实现侧应当: 高 conf 走 self.memory_manager 的 memory_write 接口写 USER.md,
        而不是绕过 Hermes 直接写文件 (这是路 B 跟路 A 的关键差别).
        """

    def on_promotion_complete(self, result: PromotionResult) -> None:
        """可选 hook · MemoryManager 调完 analyze/promote 后通知插件结果.

        默认 no-op. 子类可以重写做 logging / metrics / notification 等.
        """
        return None
