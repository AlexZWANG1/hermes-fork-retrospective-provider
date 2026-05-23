# Hermes Fork · 路 B · RetrospectiveProvider 抽象

> 给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 内核加新协议层：
> `RetrospectiveProvider` —— 让"事后回顾型记忆"成为 Hermes<b>架构里的一等公民</b>。
>
> 配套 [Habit Reflector plugin](https://github.com/AlexZWANG1/hermes-habit-reflector)
> 改成实现这个抽象的官方插件。

![Tests](https://img.shields.io/badge/tests-5%2F5_passing-brightgreen) ![Changes](https://img.shields.io/badge/Hermes_源码改动-3_文件_280行-orange) ![License](https://img.shields.io/badge/license-MIT-blue)

---

## 🎯 路 B 跟路 A 的区别（一图说清）

```
                          Hermes Agent (harness 内核)
                          ┌───────────────────────────┐
                          │                           │
                          │  Agent 主循环              │
                          │  MemoryManager            │
                          │  MemoryProvider           │
                          │  ToolDispatch             │
                          │  Hook System              │
                          │                           │
                          └─────┬─────────────────────┘
                                │
        ╔═══════════════════════╧══════════════════════════════╗
        ║                                                      ║
   ┌────▼───────┐                              ┌───────────────▼──────┐
   │ 路 A (外挂) │                              │ 路 B (这个仓库 · 内核) │
   │            │                              │                      │
   │ Hermes     │                              │ Hermes 主动调你       │
   │ 不知道你    │                              │ 注册成内核 plugin     │
   │ 存在       │  ← 这是关键差别 →             │ 改 Hermes 3 处源码    │
   │            │                              │                      │
   │ 文件 IO    │                              │ Python API           │
   │ 走后门      │                              │ 走官方协议            │
   └────────────┘                              └──────────────────────┘
```

---

## 📐 改了 Hermes 哪 3 处源码

| # | 文件 | 新增 | 干啥 |
|---|---|---|---|
| 1 | `agent/memory_provider.py` | +107 行 | 加 `Schedule` / `Claim` / `PromotionResult` / `RetrospectiveProvider` 抽象 |
| 2 | `agent/memory_manager.py` | +120 行 | 加 `run_retrospective()` 调度方法 + `get_retrospective_providers()` 查询 |
| 3 | `run_agent.py` (主循环) | +52 行 | `run_conversation()` 加触发点 + `_load_messages_for_retrospective` 方法 |

<b>总改动 280 行 · 全是追加 · 零修改旧逻辑</b>

每一处的<b>原文件 vs 改造后</b>对照在 `baseline/` 目录 vs `agent/` 目录。

---

## 📂 仓库布局

```
hermes-fork-pathB/
├── README.md                                    ← 本文件
├── baseline/                                    ← 改造前对照基线
│   ├── memory_provider.original.py              (279 行 · Hermes 原文件)
│   ├── memory_manager.original.py               (555 行 · Hermes 原文件)
│   └── run_agent.run_conversation_excerpt.py    (232 行 · 主循环节选)
│
├── agent/                                       ← 改造后的 3 个文件
│   ├── memory_provider.py                       (386 行 · 改动 1)
│   ├── memory_manager.py                        (675 行 · 改动 2)
│   └── run_agent.run_conversation_excerpt.py    (284 行 · 改动 3)
│   └── (其他 50+ 文件 symlink 到 Hermes 原仓库, 用于 import)
│
├── plugins/
│   └── memory/
│       └── habit_reflector/                     ← 路 B 的最终落点
│           ├── __init__.py                      (HabitReflector 实现 RetrospectiveProvider)
│           └── plugin.yaml                      (Hermes 插件清单)
│
├── tests/
│   └── test_pathB_integration.py                (5 个集成测试 · 全过)
│
└── docs/
    └── (改造逐行讲解, 见 README 下方)
```

---

## 🧪 跑测试

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
pip install pyyaml --break-system-packages
python3 tests/test_pathB_integration.py
```

预期输出:

```
  ✓ test_change1_retrospective_provider_subclass_works    [改动 1 抽象能用]
  ✓ test_change2_memory_manager_runs_retrospective        [改动 2 调度能用]
  ✓ test_change2_skips_when_few_messages                  [改动 2 过滤生效]
  ✓ test_change2_handles_provider_errors_gracefully       [改动 2 错误隔离]
  ✓ test_habit_reflector_plugin_end_to_end_dryrun         [整链路联动]

=== 5/5 passed ===
```

---

## 🔍 改动 1 详解 · `agent/memory_provider.py`

### 加了什么

文件末尾追加 107 行, 共 4 个新东西:

```python
@dataclass
class Schedule:
    """一个 Provider 什么时候该跑."""
    interval_hours: int = 24
    min_idle_hours: float = 1.0
    cold_start_weeks: int = 1
    allowed_hour_range: Optional[tuple] = None     # e.g. (2, 6) 只在凌晨跑


@dataclass
class Claim:
    """Retrospective 分析产出的一个'结论'."""
    claim: str                              # 一句话结论
    classification: str                     # preference/habit/task/constraint
    evidence: List[Dict[str, Any]]          # ≥3 个 session/turn 引用
    confidence: float                       # 0.0-1.0
    decay_at: str                           # ISO date
    intervention: Optional[str] = None      # 给 agent 的指令
    source: str = "retrospective"


@dataclass
class PromotionResult:
    """promote() 跑完反馈."""
    promoted_to_usermd: int = 0
    queued_for_review: int = 0
    dropped_low_conf: int = 0
    skill_candidates: int = 0


class RetrospectiveProvider(MemoryProvider):
    """事后回顾型记忆插件 · 基类."""

    @abstractmethod
    def schedule(self) -> Schedule: ...

    @abstractmethod
    def analyze(self, messages, *, window_days=30) -> List[Claim]: ...

    @abstractmethod
    def promote(self, claims) -> PromotionResult: ...

    def on_promotion_complete(self, result): ...   # 可选 hook
```

### 为什么这么设计

| 设计选择 | 理由 |
|---|---|
| 继承 `MemoryProvider` | 复用 Hermes 现有 plugin 注册 / lifecycle 机制 |
| `analyze` 接 `messages` 列表 | 由 manager 注入 (不让 provider 自己读 SQLite) |
| `promote` 返回 `PromotionResult` | 让 manager 知道每个 provider 跑出来啥, 可 metric |
| `Schedule.allowed_hour_range` | 让 provider 自己声明运行时段 (隔离调度策略) |
| `Claim` 强制 ≥3 evidence | schema-level 强约束防止低质量 claim |

---

## 🔍 改动 2 详解 · `agent/memory_manager.py`

### 加了什么

在 `initialize_all()` 后追加 120 行 · 2 个新方法:

```python
def get_retrospective_providers(self) -> List[RetrospectiveProvider]:
    """从所有 providers 里筛 RetrospectiveProvider 实例."""
    from agent.memory_provider import RetrospectiveProvider
    return [p for p in self._providers if isinstance(p, RetrospectiveProvider)]

def run_retrospective(
    self,
    *,
    trigger: str = "idle",
    force: bool = False,
    message_loader: Optional[callable] = None,
) -> Dict[str, Any]:
    """巡视所有 RetrospectiveProvider, 把该跑的跑掉.

    返回 {"ran": [...], "skipped": [...], "errors": [...]}
    """
    # 4 步: schedule check → load messages → analyze → promote
```

### 调度的 4 步

```
对每个 RetrospectiveProvider:
  ┌─────────────────────────────────────┐
  │ 第 1 步 · schedule 检查              │
  │   - allowed_hour_range 在范围内?    │
  │   - force=True 跳过检查             │
  └─────────────────────────────────────┘
                  │
                  ▼ 通过
  ┌─────────────────────────────────────┐
  │ 第 2 步 · 读 messages               │
  │   message_loader(window_days=30)    │
  │   <10 条 → skip                     │
  └─────────────────────────────────────┘
                  │
                  ▼ 通过
  ┌─────────────────────────────────────┐
  │ 第 3 步 · provider.analyze()         │
  │   出错 → errors[] · 继续下一个      │
  └─────────────────────────────────────┘
                  │
                  ▼ 通过
  ┌─────────────────────────────────────┐
  │ 第 4 步 · provider.promote()         │
  │   出错 → errors[] · 继续下一个      │
  └─────────────────────────────────────┘
                  │
                  ▼
       ran[].append({name, claims, result})
```

### 跟现有 MemoryManager 怎么协调

原 `MemoryManager` 6 个方法处理 prospective 通道:
- `build_system_prompt` · 拼 system prompt
- `prefetch_all` · 当前 turn 查询
- `sync_all` · 同步 turn 内容
- `handle_tool_call` · 路由 memory tool 调用
- `on_turn_start` / `on_session_end` · 生命周期

**改动 2 新加的 2 个方法专门处理 retrospective 通道, 跟原有 6 个并存不冲突.**

---

## 🔍 改动 3 详解 · `run_agent.py` 主循环

### 加了什么

`run_conversation()` 函数顶部 (在 `_restore_primary_runtime()` 之后) 追加 22 行:

```python
self._restore_primary_runtime()

# ★ 路 B 改动 3 · 在每个 turn 开始前给 MemoryManager 一个机会跑 retrospective
try:
    if hasattr(self, "memory_manager") and self.memory_manager is not None:
        self.memory_manager.run_retrospective(
            trigger="conversation_start",
            message_loader=self._load_messages_for_retrospective,
        )
except Exception as e:
    logger.warning("retrospective trigger at conversation_start failed: %s", e)
```

`AIAgent` 类底部追加 30 行 · 1 个新方法:

```python
def _load_messages_for_retrospective(self, *, window_days: int = 30) -> List[Dict[str, Any]]:
    """从 SessionDB 读过去 N 天的 messages 喂给 RetrospectiveProvider."""
    if not hasattr(self, "session_db") or self.session_db is None:
        return []
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()
    try:
        return self.session_db.list_messages_since(cutoff)
    except Exception as e:
        logger.warning("loading messages failed: %s", e)
        return []
```

### 为什么放在 turn 开始而不是 turn 结束

| 选择 | 理由 |
|---|---|
| ✅ **turn 开始** | 用户发新 prompt = 一定 idle 过. 此时跑 retrospective 不会跟当前 turn 抢资源, 且新 prompt 立刻能受益于刚 promote 的 USER.md 条目 |
| ❌ turn 结束 | 用户已经看到回复了, 数据已经过期 |
| ❌ idle event | Hermes 没显式 idle event; 用 conversation 开始这个隐式 idle 信号最干净 |
| ❌ 单独 cron daemon | 那就退回路 A 了 |

### 为什么用 try/except 包

retrospective 是<b>非必要功能</b>. 它跑挂了不能影响 agent 主对话. 任何 exception 只 log warning 然后继续主流程.

---

## 🔍 Plugin · `plugins/memory/habit_reflector/__init__.py`

把路 A demo 的 `distiller.py` + `promoter.py` 业务逻辑<b>wrap</b>成实现 `RetrospectiveProvider` 的子类:

```python
class HabitReflector(RetrospectiveProvider):
    def schedule(self):
        return Schedule(
            interval_hours=24, min_idle_hours=1.0, cold_start_weeks=1,
            allowed_hour_range=(2, 6),     # 凌晨 2-6 点窗口
        )

    def analyze(self, messages, *, window_days=30):
        # 跟路 A demo 一样: 调 Haiku 蒸馏 + 本地重算 confidence
        ...

    def promote(self, claims):
        # ★ 路 B 跟路 A 唯一关键差别 ★
        # 路 A: 直接 open file write USER.md (绕过 Hermes)
        # 路 B: 调 manager.handle_tool_call("memory_write", ...) (走官方协议)
        for c in claims:
            if c.confidence >= 0.85:
                self._memory_manager.handle_tool_call(
                    "memory_write",
                    {"action": "add", "target": "user", "content": entry},
                )
```

---

## 🎬 整套链路 · 在 Hermes 真实环境下会发生什么

```
用户在 Telegram 给 Hermes 发消息 (2026-06-15 上午 10:43)
                │
                ▼
gateway/platforms/telegram.py 收到, 转给 AIAgent
                │
                ▼
AIAgent.run_conversation(user_message="...")    ← run_agent.py:11419
                │
                │  ★ 改动 3 触发 ★
                ▼
self.memory_manager.run_retrospective(           ← 在 _restore_primary_runtime 之后
    trigger="conversation_start",
    message_loader=self._load_messages_for_retrospective,
)
                │
                │  ★ 改动 2 调度 ★
                ▼
MemoryManager.get_retrospective_providers()
   → 找到 1 个: habit_reflector
                │
                ▼
schedule check: allowed_hour_range=(2,6), 现在 10:43 → skip
                │
                │  (如果是凌晨触发的话:)
                ▼
message_loader(window_days=30) → 喂 60 条 messages 进来
                │
                ▼
habit_reflector.analyze(messages)                 ← 改动 1 抽象方法
   → 调 Haiku 4.5 蒸馏
   → 解析 + 本地重算 confidence
   → 返回 3 条 Claim
                │
                ▼
habit_reflector.promote(claims)
   → conf 1.00 · 调 manager.handle_tool_call("memory_write", ...)
   → conf 0.85 · 同上
   → conf 0.85 · 同上
   → habit 类 · 写 plugins/memory/habit_reflector/candidates/*.candidate
                │
                ▼
PromotionResult(promoted_to_usermd=3, skill_candidates=1)
                │
                ▼
manager.run_retrospective 返回 {"ran": [...], "errors": []}
                │
                ▼
回到 run_conversation, 继续正常处理 user_message
   ★ 此时 USER.md 已经多了 3 条 stable preference,
   ★ system prompt 注入时会自动包含
   ★ Agent 立刻"知道"用户的稳定偏好
```

---

## 📊 与路 A 的对比

| 维度 | 路 A · 外挂版 | 路 B · 这个仓库 |
|---|---|---|
| Hermes 知不知道你存在 | ❌ 不知道 | ✅ 注册成 Provider |
| 触发方式 | 外部 cron daemon | Hermes 主循环主动调 |
| 读 messages | 直接 open SQLite 文件 | manager 喂进来 |
| 写 USER.md | 直接 open file write | 调 `manager.handle_tool_call("memory_write")` |
| 跟 Hermes 一起升级 | ❌ Hermes 改 schema 就崩 | ✅ ABC 不变就兼容 |
| 算 harness 改造吗 | ❌ 算外挂工具 | ✅ 给 Hermes 加新抽象 |
| 提 PR 到上游可能性 | ❌ 没法 | ✅ 可以 |
| 测试 | 12/12 通过 | 5/5 通过 |
| 代码行数 | ~370 行业务 + ~110 行 cron | ~280 行 Hermes 改动 + ~340 行 plugin |

---

## 🚀 用法 (在 Hermes 真实环境)

```bash
# 1. fork Hermes 仓库
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent

# 2. apply 我们的 3 处改动 (用 patches/ 目录的 unified diff)
patch -p1 < /path/to/hermes-fork-pathB/patches/01-add-retrospective-provider.patch
patch -p1 < /path/to/hermes-fork-pathB/patches/02-manager-run-retrospective.patch
patch -p1 < /path/to/hermes-fork-pathB/patches/03-conversation-trigger.patch

# 3. copy plugin
cp -r /path/to/hermes-fork-pathB/plugins/memory/habit_reflector \
   plugins/memory/

# 4. 在 ~/.hermes/config.yaml 启用
cat >> ~/.hermes/config.yaml <<EOF
plugins:
  memory:
    habit_reflector:
      enabled: true
EOF

# 5. 跑 Hermes, 任何 run_conversation() 调用都会触发改动 3
hermes
```

---

## 🎯 为什么这是真正的 harness 改造

| 标准 | 是否满足 |
|---|---|
| 加新的抽象 (协议层) | ✅ `RetrospectiveProvider` ABC |
| Hermes 内核主动调度 | ✅ `MemoryManager.run_retrospective` |
| 走 Hermes 官方 API | ✅ 用 `manager.handle_tool_call("memory_write")` |
| 可以被其他 plugin 复用 | ✅ 任何"事后回顾"插件都按这个接口实现 |
| 改动是<b>追加</b>不是修改 | ✅ 0 行旧代码被改 |
| 能 PR 进 Hermes 上游 | ✅ |

---

## 📄 License

MIT
