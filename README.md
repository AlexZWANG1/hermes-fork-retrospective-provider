# Hermes RetrospectiveProvider · 给 Hermes Agent 内核加新抽象

> 给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 内核加一种新的记忆插件类型 `RetrospectiveProvider`。
> 让"<b>事后回顾型自学习</b>"成为 Hermes 架构里的一等公民——配套一个官方 plugin `habit_reflector` 演示这个抽象怎么用。

![Tests](https://img.shields.io/badge/tests-5%2F5_passing-brightgreen) ![Changes](https://img.shields.io/badge/Hermes_源码改动-3_文件_280行-orange) ![License](https://img.shields.io/badge/license-MIT-blue) ![Hermes](https://img.shields.io/badge/Hermes-内核层改造-purple)

---

## 📖 目录

- [🔥 为什么要加这个模块](#-为什么要加这个模块)
- [📦 具体加了什么](#-具体加了什么)
- [✨ 解决了什么问题](#-解决了什么问题)
- [🚀 用法](#-用法)
- [🏗️ 改造细节](#%EF%B8%8F-改造细节)
- [🧪 测试](#-测试)
- [🎬 整套链路示例](#-整套链路示例)

---

## 🔥 为什么要加这个模块

### Hermes 现有自学习机制 · 5 层全是「当下凭感觉」

Hermes 现在已经有 5 层自学习能力（[源码 reference](https://github.com/NousResearch/hermes-agent)）：

| # | 层 | 触发 | 在做什么 |
|:-:|---|---|---|
| L1 | 短期工作记忆 | 每 turn | LLM 自压缩 conversation history |
| L2 | 长期 Episodic | `session_search` tool | FTS5 关键词检索 + aux model 总结 |
| L3 | Dialectic Memory（Honcho 插件） | 每 N turn | LLM 自反思生成 peer card |
| L4 | Curated MEMORY / USER.md | `nudge` / `flush` | Agent 一拍脑袋写笔记 |
| L5 | Skill 自演化（Curator） | 每 7 天 | aux AIAgent 决定 keep/patch/archive skill |

<b>所有 5 层都有一个共同特征：</b>都是<b>prospective（当下事件触发）</b>——Agent 在 turn 进行中凭感觉决定记什么。

### 这导致 3 个具体痛点

#### 痛点 1 · 稳定偏好被反复纠正

```
2026-04-25  用户: 用表格
2026-04-27  用户: 改成 bullet 不要 prose
2026-04-30  用户: 用表格列出
2026-05-08  用户: 改成 bullet 谢谢
2026-05-23  用户: 用表格           ← 第 14 次说同一件事
```

Agent 没记进 `USER.md`。每次 nudge 时机 agent 看着<b>当前 turn</b> 觉得"嗯这次用户提到要表格了"，但 14 次中只命中 nudge 2-3 次。<b>USER.md 主要被'用户在做笔试题'这种 task state 占走了。</b>

#### 痛点 2 · 高频短任务永远沉淀不下来

Hermes 的 autonomous skill creation 触发条件是 `≥5 tool_call + agent 自评成功`。但"读 paper 出表格"这类高频任务往往 4 turn 就完成 —— <b>永远卡在门槛外，做 12 次都不会变成 skill</b>。

#### 痛点 3 · USER.md 没有过期机制

USER.md 上限 500 字符。`nudge` 时 agent 一拍脑袋写的条目：
- "用户在做 SOTA harness 笔试题"
- "用户在飞机上离线阅读"
- "用户喜欢边讨论边出 HTML"

这些都是<b>当下 task state</b>。一个月后笔试题做完了，这些条目还在小本子上占字符。<b>没有任何机制让它们自动过期</b>，agent 也不会主动清。

### 根本原因 · 缺一种「事后回顾」机制

5 层都是"当下处理"——<b>没有任何一层负责回头看一段历史，找反复出现的稳定模式</b>。

要解决以上 3 个痛点，需要给 Hermes 内核加一种新的能力：<b>定期 retrospective 分析过去 N 天的对话历史，蒸馏出稳定 user habit</b>。

这就是本模块要加的东西。

---

## 📦 具体加了什么

### 一句话定位

> **给 Hermes 内核加一个新的 abstract base class `RetrospectiveProvider`，让任何"事后回顾型记忆"都能按统一接口接入 Hermes 调度系统；配套一个 plugin `habit_reflector` 演示它怎么工作。**

### 3 处源码改动 + 1 个 plugin

| 改动 | 文件 | 新增行数 | 干啥 |
|:-:|---|:-:|---|
| **改动 1** | `agent/memory_provider.py` | +107 | 加 `RetrospectiveProvider` ABC + `Schedule` / `Claim` / `PromotionResult` 数据类 |
| **改动 2** | `agent/memory_manager.py` | +120 | 加 `run_retrospective()` 调度方法 + `get_retrospective_providers()` 查询方法 |
| **改动 3** | `run_agent.py`（主循环） | +52 | `run_conversation()` 加触发点 + `_load_messages_for_retrospective` loader 方法 |
| **Plugin** | `plugins/memory/habit_reflector/` | ~340 | 实现 `RetrospectiveProvider` 的官方插件 |

<b>3 处 Hermes 源码改动总计 280 行 · 全部是追加 · 零修改任何旧逻辑。</b>

### 4 个新数据类（改动 1 引入）

```python
@dataclass
class Schedule:
    """一个 retrospective provider 什么时候该跑."""
    interval_hours: int = 24                       # 多久跑一次（默认 24h）
    min_idle_hours: float = 1.0                    # 用户 idle 多久才允许跑
    cold_start_weeks: int = 1                      # 装好后前 N 周不跑（攒数据）
    allowed_hour_range: Optional[tuple] = None     # 只在某时段允许 (e.g. (2,6) 凌晨)


@dataclass
class Claim:
    """Retrospective 分析产出的一个'结论'."""
    claim: str                              # 一句话结论
    classification: str                     # preference / habit / task / constraint
    evidence: List[Dict[str, Any]]          # ≥3 个 session_id/turn_n/excerpt 引用
    confidence: float                       # 0.0-1.0 本地公式计算
    decay_at: str                           # ISO date 何时过期
    intervention: Optional[str] = None      # 给 agent 的可选行动指令


@dataclass
class PromotionResult:
    """promote() 跑完反馈给 manager."""
    promoted_to_usermd: int = 0       # 自动写入 USER.md 的数量
    queued_for_review: int = 0        # 入待审 queue 的数量
    dropped_low_conf: int = 0         # 置信度太低被丢弃
    skill_candidates: int = 0         # 标记给 Curator 的 skill 候选


class RetrospectiveProvider(MemoryProvider):
    """事后回顾型记忆插件 · 基类."""

    @abstractmethod
    def schedule(self) -> Schedule: ...

    @abstractmethod
    def analyze(self, messages: List[Dict], *, window_days: int = 30) -> List[Claim]: ...

    @abstractmethod
    def promote(self, claims: List[Claim]) -> PromotionResult: ...

    def on_promotion_complete(self, result: PromotionResult) -> None: ...
```

### Plugin 实现 · `habit_reflector`

`plugins/memory/habit_reflector/__init__.py` 是新抽象的第一个实现——做"<b>每天回顾 30 天对话，蒸馏 user habit</b>"这件事：

```python
class HabitReflector(RetrospectiveProvider):
    """每天凌晨回顾过去 30 天对话, 抽取稳定 user habit 写入 USER.md."""

    def schedule(self) -> Schedule:
        return Schedule(
            interval_hours=24,             # 每天 1 次
            min_idle_hours=1.0,            # 用户至少 idle 1 小时
            cold_start_weeks=1,            # 装后第 1 周不跑
            allowed_hour_range=(2, 6),     # 只在凌晨 2-6 点跑
        )

    def analyze(self, messages, *, window_days=30):
        # 调 Haiku 4.5 蒸馏 messages → claims
        # 本地公式重算 confidence (不信模型自评)
        # 强制每条 claim ≥3 evidence
        return claims

    def promote(self, claims):
        # 高 conf (≥0.85) → 通过 manager 写入 USER.md
        # 中 conf (0.70-0.85) → 写 candidates/ 待审
        # habit 分类 → 额外写 skill candidate 给 Curator
        return result
```

---

## ✨ 解决了什么问题

### 解决问题 1 · 稳定偏好不再被重复纠正

```
不装 RetrospectiveProvider:
  Agent 14 次都没记进 USER.md → 用户每次都要纠正一次

装上 habit_reflector:
  · 第 5 次出现"用表格"后，Reflector 半夜分析检测到 5+ session 反复出现
  · confidence 算出 1.00 → 自动通过 manager.handle_tool_call 写入 USER.md
  · 第二天 agent 启动，system prompt 注入 USER.md 自带这条
  · 用户再说"读 paper" → agent 直接出表格，不用纠正
```

### 解决问题 2 · 高频短任务能变 skill candidate

```
不装 RetrospectiveProvider:
  4-turn 短任务永远卡在 ≥5 tool_call 门槛外 → 做 12 次还是没 skill

装上 habit_reflector:
  · Reflector 在 30 天窗口内检测到"读 paper 出表格"模式跨 5+ session
  · classification 标为 "habit" → 写 candidates/<name>.candidate 文件
  · Curator 下次 7 天周期跑时读到这个候选 → 评估并建 skill
  · 高频短任务从"永远不能 skill"→"5 个 session 后变 candidate"
```

### 解决问题 3 · USER.md 自动过期

每条 Reflector 写入的 claim 都带 `decay_at` 字段：

| 分类 | 默认 decay | 含义 |
|---|---|---|
| `preference` | 90 天 | 用户稳定偏好（用表格等） |
| `habit` | 30 天 | 高频任务模式 |
| `task` | 7 天 | 当前任务状态（不进 USER.md） |
| `constraint` | 180 天 | 用户硬约束 |

每天 Reflector 跑的时候顺便清掉过期条目——<b>USER.md 自然保持新鲜</b>，不会积累陈年噪音。

### 架构层面的价值

| 维度 | 加这个模块前 | 加这个模块后 |
|---|---|---|
| 自学习层数 | 5 层（全 prospective） | <b>5+1 层（多了 retrospective）</b> |
| Retrospective 通道 | <b>无</b> | ✅ 由 `RetrospectiveProvider` ABC 规范 |
| 第三方插件能不能加自学习 | 只能挂在现有 6 个 plugin（Honcho 等）的副作用上 | <b>可以直接实现 `RetrospectiveProvider`</b> |
| Hermes 升级 schema 时插件能不能存活 | 取决于 plugin 是否绕过官方 API | ✅ 通过 ABC 抽象保护，向前兼容 |
| 调度统一性 | 每个 plugin 自己想办法触发 | <b>`MemoryManager.run_retrospective()` 统一调度</b> |

---

## 🚀 用法

### 在 Hermes 真实环境用

```bash
# 1. fork Hermes 仓库
git clone https://github.com/NousResearch/hermes-agent
cd hermes-agent

# 2. apply 3 处改动 (后续会出 unified diff 文件)
# 暂时手动 merge: 把本仓库 agent/ 下 3 个文件的新增部分追加到 hermes-agent 对应文件末尾

# 3. copy plugin
cp -r /path/to/this-repo/plugins/memory/habit_reflector \
   plugins/memory/

# 4. 在 ~/.hermes/config.yaml 启用
cat >> ~/.hermes/config.yaml <<EOF
plugins:
  memory:
    habit_reflector:
      enabled: true
      distiller_model: claude-haiku-4-5
      window_days: 30
      auto_promote_threshold: 0.85
EOF

# 5. 跑 Hermes (任何 run_conversation 调用都会自动触发 retrospective 调度)
hermes
```

### 调用接口 · 让你的 plugin 接进 RetrospectiveProvider 抽象

```python
from agent.memory_provider import RetrospectiveProvider, Schedule, Claim, PromotionResult

class MyRetrospectivePlugin(RetrospectiveProvider):
    name = "my_plugin"

    def schedule(self):
        return Schedule(interval_hours=24, allowed_hour_range=(3, 7))

    def analyze(self, messages, *, window_days=30):
        # 你的分析逻辑
        # 返回 List[Claim]
        ...

    def promote(self, claims):
        # 你怎么处理 claims
        # 写 USER.md 用 self._memory_manager.handle_tool_call("memory_write", ...)
        return PromotionResult(promoted_to_usermd=...)
```

注册到 MemoryManager（在 `~/.hermes/config.yaml` 里加 plugin 配置）之后，<b>Hermes 主循环每次 turn 开始会自动调度你的 plugin</b>。

---

## 🏗️ 改造细节

### 改动 1 · 加 `RetrospectiveProvider` 抽象

**位置**：`agent/memory_provider.py` 文件末尾追加（line 280-386）<br>
**性质**：纯新增，不动原 `MemoryProvider` 接口

为什么继承 `MemoryProvider`：
- 复用 Hermes 现有 plugin 注册 + lifecycle 机制
- 跟 Honcho、Retaindb 等其他 memory plugin 在同一注册表里
- 用 `isinstance(p, RetrospectiveProvider)` 一行就能筛出来

### 改动 2 · `MemoryManager` 加 `run_retrospective()` 方法

**位置**：`agent/memory_manager.py` 在 `initialize_all()` 后追加（line 556-675）<br>
**性质**：纯新增，原 6 个方法（`build_system_prompt` / `prefetch_all` / `sync_all` / `handle_tool_call` / `on_turn_start` / `on_session_end`）一行不动

调度 4 步：

```
对每个 RetrospectiveProvider:
  第 1 步 · schedule 检查
    - allowed_hour_range 在范围内?
    - force=True 跳过检查
        ↓
  第 2 步 · 读 messages
    - message_loader(window_days=30)
    - <10 条 → skip
        ↓
  第 3 步 · provider.analyze()
    - 出错 → errors[] · 继续下一个
        ↓
  第 4 步 · provider.promote()
    - 出错 → errors[] · 继续下一个
        ↓
  ran[].append({name, claims_total, result})
```

错误隔离：任何一个 provider 抛错<b>不会影响</b>其他 provider 跑。

### 改动 3 · 主循环加触发点

**位置**：`run_agent.py` 的 `run_conversation()` 函数（line 11486-11507）<br>
**性质**：在 `_restore_primary_runtime()` 之后追加 22 行 + 类底部追加 30 行 loader 方法

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

为什么放在 turn 开始：

| 选择 | 理由 |
|---|---|
| ✅ **turn 开始** | 用户发新 prompt = 一定经过了一段 idle。此时跑不会跟当前 turn 抢资源，且新 prompt 立刻能受益于刚 promote 的 USER.md 条目 |
| ❌ turn 结束 | 用户已经看到回复了，太晚 |
| ❌ Hermes 没显式 idle event | 用 conversation 开始这个隐式 idle 信号最干净 |

为什么用 `try/except` 包：retrospective 是<b>非必要功能</b>，跑挂了不能影响 agent 主对话。任何 exception 只 log warning 然后继续。

---

## 🧪 测试

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
pip install pyyaml --break-system-packages
python3 tests/test_pathB_integration.py
```

预期输出：

```
  ✓ test_change1_retrospective_provider_subclass_works    [改动 1 抽象能用]
  ✓ test_change2_memory_manager_runs_retrospective        [改动 2 调度能用]
  ✓ test_change2_skips_when_few_messages                  [改动 2 过滤生效]
  ✓ test_change2_handles_provider_errors_gracefully       [改动 2 错误隔离]
  ✓ test_habit_reflector_plugin_end_to_end_dryrun         [整链路联动]

=== 5/5 passed ===
```

测试覆盖：

| 测试 | 验证什么 |
|---|---|
| `test_change1_retrospective_provider_subclass_works` | `RetrospectiveProvider` ABC 可正确被子类化 |
| `test_change2_memory_manager_runs_retrospective` | `MemoryManager.run_retrospective` 能正确调度 provider |
| `test_change2_skips_when_few_messages` | `<10` 条 messages 时跳过 analyze |
| `test_change2_handles_provider_errors_gracefully` | provider 抛错时 manager 不崩，记入 errors[] |
| `test_habit_reflector_plugin_end_to_end_dryrun` | 整链路联动：触发 → 调度 → analyze → promote → 写 USER.md |

---

## 🎬 整套链路示例

```
用户在 Telegram 给 Hermes 发消息 (2026-06-15 凌晨 04:43)
                │
                ▼
gateway/platforms/telegram.py 收到, 转给 AIAgent
                │
                ▼
AIAgent.run_conversation(user_message="...")    ← run_agent.py:11419
                │
                │  ★ 改动 3 自动触发 ★
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
schedule check: allowed_hour_range=(2,6), 现在 04:43 → ✅ 允许
                │
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
   → conf 1.00 · 调 manager.handle_tool_call("memory_write", target="user")
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

## 📂 仓库结构

```
hermes-fork-pathB/
├── README.md                                    ← 本文件
│
├── baseline/                                    ← 改造前 · Hermes 原文件对照
│   ├── memory_provider.original.py              (279 行)
│   ├── memory_manager.original.py               (555 行)
│   └── run_agent.run_conversation_excerpt.py    (232 行 · 主循环节选)
│
├── agent/                                       ← 改造后 · 3 个新增文件
│   ├── memory_provider.py                       (386 行 · +107 改动 1)
│   ├── memory_manager.py                        (675 行 · +120 改动 2)
│   └── run_agent.run_conversation_excerpt.py    (284 行 · +52 改动 3)
│
├── plugins/
│   └── memory/
│       └── habit_reflector/                     ← 新抽象的官方 plugin 实现
│           ├── __init__.py                      (HabitReflector class)
│           └── plugin.yaml                      (Hermes 插件清单)
│
└── tests/
    └── test_pathB_integration.py                (5 个集成测试, 全过)
```

---

## 📄 License

MIT
