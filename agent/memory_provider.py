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
# 改造 · 拆 RetrospectiveProvider 成两层（以下全部新增 · ~180 行）
#
# 为什么拆两层 · 短答:
#   把"agent 自动学 habit 改自己 behavior"拆成两件事:
#   1. EpisodicProvider · 全量记录 · 不改 behavior · 不漂移
#   2. ActionableHabitProvider · 蒸馏 actionable rules · 必须过 fitness firewall + 人审
#
# 为什么不能合在一起 · 第一性:
#   GEPA / habit_reflector 类方案的两个 fundamental 问题:
#     (11.1) "学习不是万能" · 自动学的 habit 在新领域反而是误导
#     (11.2) "行为漂移" · agent silent self-modify, 用户无法 audit / revert
#   把"记录"和"改行为"耦合 → 这两个问题无解
#   拆开 → episodic 层不改 behavior 不漂移 · actionable 层有 firewall
#
# 详见: README.md "为什么拆两层"
# ═════════════════════════════════════════════════════════════════════════════

from dataclasses import dataclass, field


@dataclass
class Episode:
    """一次 user-agent 交互的全量记录 · 不做信噪比判断."""

    episode_id: str                              # uuid
    session_id: str
    turn_n: int
    timestamp: float                             # unix ts
    user_content: str                            # 用户原始 prompt
    assistant_content: str                       # agent 回复
    context_refs: List[str] = field(default_factory=list)   # 引用的过往 episode_id
    outcome: Optional[str] = None                # 用户后续反馈 (改正/认可/重试 etc), 可选
    tags: List[str] = field(default_factory=list)
    user_pinned: bool = False                    # 用户显式 pin · 永远顶在 retrieve 前面


@dataclass
class HabitCandidate:
    """从 episodes 蒸馏出的、待审的 actionable rule (尚未注入 USER.md)."""

    candidate_id: str                            # uuid
    claim: str                                   # 一句话规则
    classification: str                          # preference / habit / constraint
    supporting_episodes: List[str]               # ≥3 个 episode_id
    raw_confidence: float                        # 蒸馏时的初始打分 · 仅作排序
    proposed_at: str                             # ISO timestamp
    intervention: Optional[str] = None
    fitness_report: Optional[dict] = None        # ① 必须通过 fitness 才能 promote
    user_approved: bool = False                  # ② 必须用户显式审批


@dataclass
class FitnessReport:
    """A/B fitness pipeline 跑出来的结果."""

    candidate_id: str
    win_rate_B: float                            # B (with habit) 胜过 A (no habit) 比例
    verdict: str                                 # "pass" / "fail" / "neutral"
    n_samples: int = 0
    notes: str = ""
    judge_model: str = ""
    transcript_refs: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Layer A · EpisodicProvider
#
# 职责: 全量记录交互 · 不改 behavior · 只在 query 时浮现
# 关键不变量:
#   - record() 是 default-on · 永远写入 · 不在写入时做价值判断
#   - 不出现 promote / inject_to_system_prompt 任何方法
#   - 任何 implementer 都不应该把 retrieve() 结果<b>主动</b>塞进 system prompt
#   - retrieval 结果作为 reference 给用户/agent 显式引用, 由用户/agent 决定是否参考
# ─────────────────────────────────────────────────────────────────────────────


class EpisodicProvider(MemoryProvider):
    """全量记录 user-agent 交互的 provider · Layer A.

    设计原则:
      1. Write is cheap: record() 默认对每个 turn 调用, 不做价值判断
      2. Read is smart: retrieve() 用 multi-signal ranking 浮现相关 episodes
      3. No behavior modification: episode 不改 agent system prompt, 只作 reference
      4. No drift risk: 因为不改 behavior, 不存在漂移问题
    """

    @abstractmethod
    def record(self, episode: Episode) -> None:
        """全量记录一次交互. 默认每个 turn 都调.

        实现侧应当: 写盘 / 写 DB · 不调 LLM · 不判断价值.
        """

    @abstractmethod
    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        session_id: Optional[str] = None,
    ) -> List[Episode]:
        """检索相关 episodes · 由用户/agent 显式查询时触发.

        实现侧应当: BM25 / embedding / multi-signal ranking 都行.
        关键: 不能把结果<b>主动注入</b> system prompt · 必须是 pull 模型不是 push.
        """

    def pin(self, episode_id: str) -> None:
        """用户显式 pin 一个 episode · 检索时永远顶前面."""

    def unpin(self, episode_id: str) -> None:
        """取消 pin."""


# ─────────────────────────────────────────────────────────────────────────────
# Layer B · ActionableHabitProvider
#
# 职责: 从 episodes 蒸馏 actionable rules · 必须过 fitness gate + 人审才 promote
# 关键不变量:
#   - propose_habit() 只产生 candidate · 不直接写 USER.md
#   - fitness_test() 必须跑 A/B 评估 · verdict pass 才进入下一步
#   - 进入 USER.md 必须经过 await_user_approval() · 不能 silent self-modify
#   - 每次 promote 都留 audit trail · 永远可 revert
# ─────────────────────────────────────────────────────────────────────────────


class ActionableHabitProvider(MemoryProvider):
    """从 episodes 蒸馏 actionable rules · Layer B.

    设计原则:
      1. Promotion is conservative: 只少数 candidates 升级
      2. Fitness firewall mandatory: 必须 A/B 测试通过 (回应 Decagon "fitness function firewall")
      3. Human in loop mandatory: 必须用户显式审批 (回应"行为漂移"问题)
      4. Reversibility: 每条已 promote 的 habit 都可一键 revert + 黑名单
    """

    @abstractmethod
    def propose_habit(
        self,
        episodes: List[Episode],
    ) -> List[HabitCandidate]:
        """从 episodes 中蒸馏出 candidate habits · 不直接 promote.

        实现侧: 调 LLM (Haiku 等) 找 ≥3 个 episode 反复出现的 stable pattern.
        关键: 返回 HabitCandidate · 不调 memory_tool · 不改 USER.md.
        """

    @abstractmethod
    def fitness_test(
        self,
        candidate: HabitCandidate,
        sample_queries: List[str],
    ) -> FitnessReport:
        """A/B 评估: candidate habit 注入 vs 不注入, LLM judge 评分.

        实现侧:
          ① 对 sample_queries 跑两次 agent:
             A: 不注入 candidate, B: 注入 candidate
          ② LLM-as-judge 评 A/B 哪个回复更贴当下 query
          ③ 计算 win_rate_B 与 verdict (pass/fail/neutral)
        """

    @abstractmethod
    def await_user_approval(
        self,
        candidate: HabitCandidate,
    ) -> bool:
        """提交 candidate 给用户审批 · 返回 True 表示用户批准.

        实现侧: 写 .pending.json + 通知用户审 (CLI / notification 都可)
        关键: 不能自动 approve · 必须用户显式动作.
        """

    @abstractmethod
    def promote(self, candidate: HabitCandidate) -> None:
        """把已审批的 candidate 写入 USER.md.

        前置条件: candidate.fitness_report.verdict == "pass" AND candidate.user_approved
        实现侧: 通过 self.memory_manager.handle_tool_call("memory_write", ...) 走官方 API.
        必须写 audit log: 哪条 habit · 何时 · fitness 报告 · 用户批准时间.
        """

    @abstractmethod
    def revert(self, candidate_id: str) -> None:
        """从 USER.md 删除一条已 promote 的 habit · 加入黑名单.

        关键: 用户随时可调 · 一键回到 promote 前状态.
        """


# ─────────────────────────────────────────────────────────────────────────────
# Schedule (两层共用的调度描述)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Schedule:
    """provider 什么时候被 manager 调度."""

    # 全量记录类 provider (EpisodicProvider): 每 turn 都调, 这个字段无用
    # 蒸馏类 provider (ActionableHabitProvider): 多久跑一次 propose
    interval_hours: int = 24

    # 用户必须 idle 多久才能跑蒸馏 (避免打扰交互)
    min_idle_hours: float = 1.0

    # 装好后前 N 周不跑蒸馏 (Episodic 层不受这个限制, 永远记)
    cold_start_weeks: int = 1

    # 在 idle 触发时强制的小时区间 (None = 任何时段都行)
    allowed_hour_range: Optional[tuple] = None
