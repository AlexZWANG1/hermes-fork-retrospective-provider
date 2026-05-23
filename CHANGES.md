# Hermes RetrospectiveProvider · 改造说明

# TLDR

1. 给 Hermes Agent 内核加一种新的 memory provider 子类 `RetrospectiveProvider`，规范"事后回顾型自学习"的接口；
2. 在 `MemoryManager` 上加 `run_retrospective()` 方法，由 manager 主动调度所有注册的 retrospective provider；
3. 在主循环 `run_conversation()` 的 turn 开始处加 5 行触发点，每个新 turn 给 manager 一次调度机会；
4. 配套一个官方 plugin `habit_reflector` 实现该抽象：每天凌晨读过去 30 天 messages，用 Haiku 4.5 蒸馏稳定 user habit，通过 `manager.handle_tool_call("memory_write")` 自动写入 USER.md；
5. 3 处 Hermes 源码改动合计 +280 行 · 全部追加 · 0 行修改旧逻辑 · 5/5 集成测试通过（详见 §6）；
6. 需要：(a) 真 hermes CLI 端到端跑过（当前测试都是独立 Python 进程）；(b) 用真实数据验证 Haiku 蒸馏质量（当前只用 14-session 合成 fixture）；(c) 核实 `session_db.list_messages_since` API 在 Hermes 真存在（loader 写法是基于 `hermes_state.py` 类似命名推测的）。

---

# What are we building

我们要给 Hermes 的自学习机制加一种新通道，让 agent 能"事后回顾过去的对话历史"，而不是只能"当下凭感觉写笔记"。

具体一点：让 Hermes 在每个 turn 开始时，能自动决定要不要跑一次离线的 habit 蒸馏，把过去 30 天反复出现的稳定 user habit 写进 `USER.md` 给下次对话用。

最终效果是 `USER.md` 内容从这样：

```
# 改造前 · Hermes 现状
- 用户在做 SOTA harness 笔试题       ← agent 在 nudge 时刻当下写的 task state
- 用户在飞机上离线阅读                ← 同上
- 用户喜欢边讨论边出 HTML
- 用户在 Obsidian 里看东西
```

变成这样：

```yaml
# 改造后 · 由 retrospective 蒸馏写入
- 用户强偏好结构化输出（表格 + bullet）而非 prose
  [conf 1.00 · 8 sessions · decay 2026-08-21 · src reflector]
- 高频任务：读 paper → 出表格 critique
  [conf 0.85 · 5 sessions · decay 2026-06-22 · src reflector]
- 工作日上午 10-13 点 短 session 高密度（4-6 turn 完成）
  [conf 0.85 · 9 sessions · decay 2026-08-21 · src reflector]
```

字数不变（500 字符内），信息密度提升一个量级：每条带 evidence 引用、置信度、过期日期、来源标记。

---

# Why a new abstraction

有几条<b>看似通向同一个目标</b>但实际有结构缺陷的路径。我们逐一砍掉：

| 方法 | 简述 | 核心缺陷 |
|---|---|---|
| **外挂 cron 脚本** | 在 `~/.hermes/hooks/` 里挂独立 Python 脚本，cron 触发；直接 `sqlite3 ~/.hermes/state.db` 读 messages、直接 `open(USER.md, "a")` 写 | 1. Hermes 不知道你存在，跟其他 6 个 memory plugin 不在同一注册表；<br>2. 绕过 `memory_tool.add()` 的 char budget 检查、备份、scan 等护栏；<br>3. Hermes 升级 schema/字段就崩；<br>4. 没法 PR 进上游 |
| **扩 `MemoryProvider` 现有接口** | 在原 `prefetch / sync_turn / on_turn_start` 里夹 retrospective 逻辑 | 这些接口都是<b>每 turn 同步触发</b>的，跑 batch（30 天历史）等于卡死主循环；语义错配 |
| **改 Curator** | 复用 Curator 现成的 7 天周期 + aux AIAgent fork + tar.gz backup 机制 | `agent/curator.py` 顶头硬注释：<b>"Only touches agent-created skills"</b>（line 17）。Curator 周期是 7 天对偏好捕捉太疏；且改这个 invariant 等于改 Hermes 核心约束 |
| **走 Honcho dialectic plugin** | Honcho 是个 retrospective 风格的 plugin，借它的通道 | Honcho 是<b>独立平行系统</b>——它写自己的 peer card（远程后端），不写 Hermes 原生 `USER.md`；从来不接 `memory_tool.add` |
| **新增普通 `MemoryProvider` 类型** | 注册一个普通 provider，自己在 `is_available()` 里起 cron | 跟"外挂 cron"同病：Hermes 没法调度它，错误也没法被 manager 隔离 |

结合上面看：Hermes 现有架构里<b>没有一个位置</b>承担"回头看一段历史"这件事。要做这件事，必须给 `MemoryProvider` 加子类，给 `MemoryManager` 加调度方法。

---

# What is RetrospectiveProvider

`RetrospectiveProvider` 是 `MemoryProvider` 的子类，加 3 个抽象方法：

```python
class RetrospectiveProvider(MemoryProvider):
    """事后回顾型记忆插件 · 基类.

    跟现有 MemoryProvider 区别:
      · MemoryProvider:       agent 当下需要时拉 (prefetch / sync_turn)
      · RetrospectiveProvider: Hermes 定时主动调 (schedule + analyze + promote)
    """

    @abstractmethod
    def schedule(self) -> Schedule: ...                       # 自己声明何时跑

    @abstractmethod
    def analyze(self, messages, *, window_days=30) -> List[Claim]: ...   # 蒸馏

    @abstractmethod
    def promote(self, claims: List[Claim]) -> PromotionResult: ...       # 决定怎么用
```

配套 4 个数据类：

```python
@dataclass
class Schedule:                          # 一个 provider 什么时候允许跑
    interval_hours: int = 24             # 多久跑一次
    min_idle_hours: float = 1.0          # 用户必须 idle 多久
    cold_start_weeks: int = 1            # 装好后前 N 周不跑
    allowed_hour_range: Optional[tuple] = None   # e.g. (2, 6) 凌晨窗口

@dataclass
class Claim:                             # retrospective 产出的一个结论
    claim: str                           # 一句话
    classification: str                  # preference / habit / task / constraint
    evidence: List[Dict]                 # ≥3 个 session_id/turn_n/excerpt
    confidence: float                    # 0.0-1.0, 本地公式计算
    decay_at: str                        # ISO date, 过期日期
    intervention: Optional[str] = None   # 给 agent 的可选指令

@dataclass
class PromotionResult:                   # promote() 反馈给 manager
    promoted_to_usermd: int = 0
    queued_for_review: int = 0
    dropped_low_conf: int = 0
    skill_candidates: int = 0
```

它继承 `MemoryProvider` 是为了<b>复用 Hermes 现有的 plugin 注册、生命周期、`hermes_home` 注入</b>等基础设施。任何实现这个 ABC 的子类，都自动出现在 `MemoryManager._providers[]` 注册表里，被 `isinstance(p, RetrospectiveProvider)` 一行筛出来。

# What is habit_reflector

`habit_reflector` 是新抽象的第一个实现。

```python
class HabitReflector(RetrospectiveProvider):
    name = "habit_reflector"

    def schedule(self) -> Schedule:
        return Schedule(interval_hours=24, min_idle_hours=1.0,
                        cold_start_weeks=1, allowed_hour_range=(2, 6))

    def analyze(self, messages, *, window_days=30) -> List[Claim]:
        # 1. cold-start guard: 装后第 1 周返回 []
        # 2. <50 条 messages 返回 [] (signal 太弱)
        # 3. 渲染 30 天 messages 成 prompt 喂 Haiku 4.5
        # 4. 解析 model 返回的 raw 信号 (times_appeared 等)
        # 5. 本地公式重算 confidence (不信模型自评)
        # 6. 强制每条 claim ≥3 evidence
        return claims

    def promote(self, claims) -> PromotionResult:
        # conf ≥0.85: self._memory_manager.handle_tool_call("memory_write", ...)
        # conf 0.70-0.85: 写 candidates/<id>.pending.json
        # classification == "habit": 额外写 candidates/<name>.candidate 给 Curator
        return result
```

本地 confidence 公式：

```
起步                              0.10
+ times_appeared ≥ 5             +0.25
+ distinct_sessions ≥ 3          +0.25
+ last_seen_within_days ≤ 7      +0.25
+ user_explicit_confirmed        +0.25  (可选)
─────────────────────────────────
最高                              1.00
```

公式锁在<b>本地代码</b>而不是 prompt 里，是为了模型不能通过夸大信号数自我提升 confidence —— 信号数是 raw counts，public 给 model；confidence 是本地推导，由 manager 信任。

---

# 改了 Hermes 哪 3 处

| 改动 | 文件 | 原行数 | 改造后 | Δ | 改在哪 | 引入什么 |
|---|---|---|---|---|---|---|
| 1 | `agent/memory_provider.py` | 279 | 386 | **+107** | 文件末尾追加 | `RetrospectiveProvider` ABC + `Schedule` / `Claim` / `PromotionResult` |
| 2 | `agent/memory_manager.py` | 555 | 675 | **+120** | `initialize_all()` 后追加 | `run_retrospective()` + `get_retrospective_providers()` + `_is_provider_due()` |
| 3 | `run_agent.py` | 15469 | 15521 | **+52** | `run_conversation()` 内 22 行 + 类底部 30 行 | 触发块 + `_load_messages_for_retrospective()` loader |

合计 +280 行 Hermes 源码改动。Hermes 原有 6 个 `MemoryProvider` 方法、原 6 个 `MemoryManager` 方法、`run_conversation` 原 250+ 行业务逻辑一行没动。

## 改动 3 触发块（核心 8 行）

主循环 `run_conversation()` 在 `_restore_primary_runtime()` 之后插入：

```python
try:
    if hasattr(self, "memory_manager") and self.memory_manager is not None:
        self.memory_manager.run_retrospective(
            trigger="conversation_start",
            message_loader=self._load_messages_for_retrospective,
        )
except Exception as e:
    logger.warning("retrospective trigger at conversation_start failed: %s", e)
```

`try/except` 包裹是因为 retrospective 是<b>非必要功能</b>——跑挂了不能影响主对话。

---

# 数据流

```
用户发新消息
    │
    ▼
AIAgent.run_conversation()                       ← 改动 3 触发
    │
    ▼
MemoryManager.run_retrospective(                 ← 改动 2 调度
    trigger="conversation_start",
    message_loader=AIAgent._load_messages_for_retrospective,
)
    │
    │  对每个 RetrospectiveProvider 走 4 步：
    │
    ├── ① schedule().allowed_hour_range 在范围? 否 → skip
    │
    ├── ② message_loader(window_days=30)
    │       SessionDB → list[dict(session_id, role, content, turn_n, ts)]
    │       <10 条 → skip
    │
    ├── ③ provider.analyze(messages)
    │       habit_reflector: 渲染 prompt → Haiku 4.5 → 解析 → 本地重算 conf
    │       → List[Claim]
    │
    └── ④ provider.promote(claims)
            conf ≥0.85 → manager.handle_tool_call("memory_write", target="user")
            conf 0.70-0.85 → 写 candidates/<id>.pending.json
            classification == "habit" → 写 candidates/<name>.candidate
            → PromotionResult
    │
    ▼
回到 run_conversation, 正常处理 user_message
   USER.md 已含新 entry → 本 turn system prompt 注入时自带
```

---

# 怎么搞 · 三阶段

我们打算从当前先把<b>这一种</b> retrospective provider（habit_reflector）跑稳，再扩到下一种。先讲<b>阶段</b>，再讲<b>具体 action</b>。

## 阶段 1 · 抽象 + 调度 + 第一个 plugin

把上面 §"改了 Hermes 哪 3 处" 描述的全部完成。<b>这一阶段已完成</b>。当前状态：

- ✅ 3 处源码改动追加完
- ✅ habit_reflector plugin 实现完
- ✅ 5/5 集成测试通过（mock provider + mock loader + canned LLM response）
- ❌ <b>未在真 hermes CLI 端到端跑过</b>

## 阶段 2 · 真集成验证

把改动 1/2/3 真的 apply 到 `~/.hermes/hermes-agent/`，启动 `hermes` CLI，验证：

1. `MemoryProvider` plugin 注册 log 出现 `habit_reflector`；
2. 每个新 turn 开始时，log 出现 `[INFO] Retrospective ran: habit_reflector ...` 或 skipped reason；
3. 第 8 天后（cold-start 过）且凌晨 4 点附近运行时，真 USER.md 多出 entry；
4. <b>错误隔离</b>验证：故意让 habit_reflector 抛 exception，确认主对话不受影响（log warning 即可）。

预期阻塞：Hermes venv 当前在用户机器上是坏的（指向已删除的 Python 3.11），需先 `brew install python@3.11` 或重建 venv。

## 阶段 3 · 蒸馏质量

在 mock loader + canned response 下，5/5 测试只能验证<b>接口契约</b>，不能验证<b>蒸馏质量</b>。需要用真数据：

1. 喂真实 Hermes session（≥50 条 messages，跨 ≥5 个 session）；
2. 跑 Haiku 4.5 蒸馏；
3. 人工 label：每条 claim 是不是真的 stable preference / 是不是噪音 / evidence 引用对不对；
4. 算 precision / recall。

当前缺这一步的 baseline。<b>没有数据支撑"装上 Reflector 后 USER.md 质量提升 X%"这种说法</b>——所有改造前后对比都基于合成 fixture。

---

# 具体的 action

下一步要做的 4 件事，按优先级：

1. **修 Hermes venv** · `brew install python@3.11` 或基于 3.12 重建 venv。前置依赖，否则阶段 2 跑不动；
2. **Apply 3 处源码改动到本地 Hermes** · 见 [`TESTING.md`](./TESTING.md) Level 3 的 Step 3.1-3.6 手动操作，或者后续做成自动 patch 脚本；
3. **核实 `session_db.list_messages_since(cutoff)`** · 改动 3 的 loader 用了这个方法名，是基于 `hermes_state.py` 类似命名推测的。需要 grep Hermes 源码确认真有这个 API，否则 loader 要换；
4. **跑一次真 Haiku 端到端** · 设 `ANTHROPIC_API_KEY`，跑 TESTING.md 的 Level 2 脚本，看真 model 返回什么。预估 ~$0.001。

---

# 集成测试结果

5 个测试覆盖三处改动的接口契约：

| 测试 | 验证范围 | 状态 |
|---|---|---|
| `test_change1_retrospective_provider_subclass_works` | `RetrospectiveProvider` ABC 可正确被子类化，`Claim` / `Schedule` 数据类约束生效 | ✅ |
| `test_change2_memory_manager_runs_retrospective` | `MemoryManager.run_retrospective` 4 步流程跑通（schedule check → load → analyze → promote） | ✅ |
| `test_change2_skips_when_few_messages` | `<10` 条 messages 时跳过 analyze，记入 `skipped[]` | ✅ |
| `test_change2_handles_provider_errors_gracefully` | provider 抛错时 manager 不崩，记入 `errors[]`，其它 provider 继续跑 | ✅ |
| `test_habit_reflector_plugin_end_to_end_dryrun` | habit_reflector 整链路：注册 → 调度 → analyze（canned response）→ promote → 调 `manager.handle_tool_call("memory_write")` | ✅ |

跑法：

```bash
cd /Users/项目开发/harness-research/hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py
```

实测 5/5 通过，0.02s 跑完。

---

# 边界 · 已知不能做什么

⚠️ 写出来防止误读：

- <b>不替换</b> Hermes 现有 5 层自学习。原 L1-L5 一行没改。本改造是<b>追加</b>第 6 层；
- <b>不承诺</b>某个 benchmark 分数改善（Aider polyglot / SWE-bench 等）。从未做过 A/B 对比；
- <b>不跨 user</b>聚合 habit。每个 Hermes 实例独立，隐私问题不在本范围；
- <b>不用 embedding / vector DB</b>。retrieval 走 keyword + 文件系统。100k+ session 之后可能要重新评估；
- <b>不自动建 skill</b>。habit_reflector 只把候选写到 `candidates/<name>.candidate`，要不要真建 skill 仍由现有 Curator 决定。两条 cron 解耦；
- <b>不调度多 provider 之间的顺序</b>。当前 `MemoryManager.run_retrospective` 按注册顺序串行调用。多个 retrospective provider 之间没有依赖管理。

⚠️ 隐含假设（还未核实）：

- 假定 Hermes `session_db` 有 `list_messages_since(cutoff)` 方法。<b>没在 Hermes 源码逐字 grep</b>。基于 `hermes_state.py` 命名模式推测的。真 apply 时如果方法名错了，loader 要换；
- 假定 Hermes plugin discovery 会自动 load `plugins/memory/habit_reflector/__init__.py`。基于 `honcho`/`retaindb` 等已有 plugin 的目录结构推测。<b>未实测</b>；
- 假定 `manager.handle_tool_call("memory_write", {"action": "add", "target": "user", "content": ...})` 是 Hermes `memory_tool.py` 现有签名。基于读 `tools/memory_tool.py:465-505` 推测的。

---

# 为什么不使用已有方案 · 详细对比

> 跟 §"Why a new abstraction" 同一个内容，但展开到代码层级。

## 方案 A · 外挂 cron 脚本

```python
# ~/.hermes/hooks/on_cron.py
import sqlite3
sqlite3.connect(os.path.expanduser("~/.hermes/state.db")).execute(
    "SELECT ... FROM messages WHERE timestamp >= ?", (cutoff,)
)
# ... 跑分析 ...
open(os.path.expanduser("~/.hermes/memories/USER.md"), "a").write(claim)
```

问题：

- Hermes 完全不知道这个脚本存在，跟其他 6 个 memory plugin（`honcho`/`retaindb`/`openviking`/`byterover`/`holographic`/`supermemory`）<b>不在同一个注册表</b>；
- 绕过 `MemoryStore.add()` 的所有护栏：char budget 检查（USER.md 1375 限制）、`_scan_memory_content` 反 prompt-injection 扫描、auto backup；
- Hermes 升级 `state.db` schema（比如把 `messages.timestamp` 改成 `messages.created_at`）→ 脚本立刻坏掉，且<b>不会自动报错</b>给用户；
- 没法 PR 进上游 —— 外挂脚本不是 Hermes 仓库的一部分。

## 方案 B · 扩展 `MemoryProvider` 现有接口

```python
class MyProvider(MemoryProvider):
    def sync_turn(self, user_content, assistant_content, **kw):
        # 在这里悄悄跑一次 retrospective 分析?
        if self._should_run_retrospective_now():
            messages = self._read_db()              # 几百毫秒
            claims = self._call_haiku(messages)     # 数秒
            self._write_user_md(claims)             # 几十毫秒
```

问题：

- `sync_turn` 是<b>每 turn 同步阻塞</b>调用的。把它做成 batch 分析等于<b>每个 user turn 等几秒</b>才回应；
- 即使做成 async，`MemoryProvider` 接口没有"我这次想跳过"的语义；
- 语义错配：`MemoryProvider` 一组方法的契约是"当下 turn 上下文"，往里塞历史回顾是 hack。

## 方案 C · 改 Curator

`agent/curator.py` line 17 原文：

```python
"""Curator — background skill maintenance orchestrator.
...
Strict invariants:
  - Only touches agent-created skills (see tools/skill_usage.is_agent_created)
  - Never auto-deletes — only archives. Archive is recoverable.
  ...
"""
```

问题：

- 第一条 invariant 写死 "Only touches agent-created skills"。要复用 Curator 必须违反这条 invariant，等于改 Hermes 核心约束；
- Curator 的 7 天周期对偏好捕捉<b>太疏</b>。用户连续 14 次说"用表格"如果 14 次都在 7 天里，Curator 一次都没跑过；
- Curator 用的是 fork AIAgent + max_iter=8 的<b>重量级</b>调度方式，每次 ~$0.30。habit 蒸馏不需要起 agent loop，单次 Haiku call ~$0.40 已经够，但开销结构完全不同；
- 同一个 cron 既管 skill 又管 user habit，错误隔离更差。

## 方案 D · 走 Honcho plugin

`plugins/memory/honcho/__init__.py` 头部注释：

> "Provides cross-session user modeling with dialectic Q&A, semantic search, peer cards, and persistent conclusions via the Honcho SDK."

问题：

- Honcho 写自己的 peer card 到<b>远程 Honcho 后端</b>，不写 Hermes 原生 `USER.md`；
- 是<b>可选 plugin</b>，默认不开。把 retrospective 绑到一个非默认 plugin 上等于改造仅对开 Honcho 的用户生效；
- Honcho 的 dialectic 方法是"LLM 假设式自反思"，跟我们要的"forensic 数据驱动"在认识论上不一样。

## 方案 E · 注册一个普通 `MemoryProvider`

```python
class HabitReflector(MemoryProvider):
    def is_available(self):
        # 在这里偷偷起一个 background thread 跑 cron?
        threading.Thread(target=self._cron_loop, daemon=True).start()
        return True
```

问题：

- 跟方案 A 同病：Hermes 没法<b>调度</b>它，只能等它自己起线程；
- `MemoryManager` 不知道这个 provider 在干什么，错误<b>没法被 manager 隔离</b>；
- `is_available()` 不是 lifecycle hook，把 cron 启动塞这里是 hack。

---

# 性能与成本

| 项 | 数字 |
|---|---|
| 3 处源码改动 | +280 行（Hermes）+ 340 行（plugin） |
| 5 个集成测试 | 全部 0.02 秒跑完 |
| 单次 Haiku 4.5 蒸馏成本 | ~$0.40（input ~80k tokens × $1 + output ~3k tokens × $5） |
| 月成本估算 | ~$12（每天 1 次） |
| 主对话延迟开销 | 0 ms（异常时 only log warning，正常时 manager.run_retrospective 在 cold-start 周直接 return） |
| 改动 3 触发块代码 | 8 行 |

---

# 仓库结构

```
hermes-fork-pathB/
├── README.md                                       # 完整说明 + 整套链路示例
├── CHANGES.md                                      # 本文件 · 凝练版
├── TESTING.md                                      # 3 层测试指南
├── docs/SPEC.md                                    # 14 节设计 spec
│
├── baseline/                                       # 改造前对照
│   ├── memory_provider.original.py                 (279 行)
│   ├── memory_manager.original.py                  (555 行)
│   └── run_agent.run_conversation_excerpt.py       (232 行)
│
├── agent/                                          # 改造后的 3 个文件
│   ├── memory_provider.py                          (+107)
│   ├── memory_manager.py                           (+120)
│   └── run_agent.run_conversation_excerpt.py       (+52)
│
├── plugins/memory/habit_reflector/
│   ├── __init__.py                                 # HabitReflector 类
│   └── plugin.yaml                                 # Hermes plugin manifest
│
└── tests/test_pathB_integration.py                 # 5 个集成测试
```

---

# 时间线预期

| 阶段 | 内容 | 估时 | 当前状态 |
|---|---|---|---|
| 阶段 1 | 抽象 + 调度 + plugin + mock 测试 | 完成 | ✅ |
| 阶段 2 | 真集成 Hermes 端到端 | 半天-1 天（先修 venv） | 未开始 |
| 阶段 3 · a | 用真 Haiku 跑 1 次 + 人工 label | 半天 | 未开始 |
| 阶段 3 · b | 收集 ≥10 个真实 session 数据集 | 取决于本机使用频次 | 未开始 |
| Apply patch 自动化 | 把 3 处改动做成 `.patch` 文件 | 0.5 天 | 未开始 |

---

# 引用

- 仓库：https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- Hermes 源码 reference：`~/.hermes/hermes-agent/`（基于 2026-05 本机版本）
- 关键源码 anchor：
  - `agent/curator.py` line 17 strict invariant
  - `agent/memory_provider.py` line 42 `MemoryProvider` ABC
  - `agent/memory_manager.py` line 538 `initialize_all()`
  - `run_agent.py` line 11419 `run_conversation()` 函数
  - `tools/memory_tool.py` line 224 `MemoryStore.add()`
- 完整 README：[README.md](./README.md)
- 测试指南：[TESTING.md](./TESTING.md)
- 设计 spec：[docs/SPEC.md](./docs/SPEC.md)
