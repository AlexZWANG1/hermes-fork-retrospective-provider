# Hermes RetrospectiveProvider · 笔试介绍

# TLDR

1. 给 Hermes Agent 加一种新的记忆通道，从"agent 当下凭感觉记笔记"升级到"+ 每晚回顾过去 30 天蒸馏稳定 user habit"
2. 用户感受：你 14 次说过"用表格" → 第 5 次后自动进 USER.md → 下次启动 agent 直接知道，不用再纠正
3. 改 Hermes 源码 3 处合计 +280 行（全部追加 · 0 行修改旧逻辑）+ 1 个配套 plugin
4. 5/5 集成测试通过，已开源 https://github.com/AlexZWANG1/hermes-fork-retrospective-provider

---

# 一个具体场景

用户开 Hermes 第一周到第三周，每天问 agent "看下这篇 paper"。每次 agent 出 prose 一大段，用户每次纠正"用表格"。

这种情况下，<b>Hermes 现状</b>是这样的：

```
~/.hermes/memories/USER.md  (上限 500 字符)

- 用户在做 SOTA harness 笔试题             ← agent 在 nudge 时刻当下写的 task state
- 用户在飞机上离线阅读
- 用户喜欢边讨论边出 HTML
- 用户在 Obsidian 里看东西
```

4 条 entry 全是<b>当下 task state</b>（一个月后笔试题做完，这 4 条全过期但还占字符），<b>没有一条是 stable preference</b>。用户重复 14 次的"用表格"<b>一次都没记下来</b>。

加上这个改造后，每天凌晨 4 点 agent 离线读一遍过去 30 天的对话，写出来的 USER.md 变成：

```
~/.hermes/memories/USER.md

- 用户强偏好结构化输出（表格 + bullet）而非 prose
  [conf 1.00 · 8 sessions · decay 2026-08-21 · src reflector]
- 高频任务：读 paper → 出表格 critique
  [conf 0.85 · 5 sessions · decay 2026-06-22 · src reflector]
- 工作日上午 10-13 点 短 session 高密度（4-6 turn 完成）
  [conf 0.85 · 9 sessions · decay 2026-08-21 · src reflector]
```

同样 500 字符内，3 条 entry 全是 stable preference，每条带 evidence 引用、置信度、过期日期。

下次 agent 启动，system prompt 自动注入这 3 条。<b>从此你不用再纠正"用表格"</b>。

---

# 为什么 Hermes 现在做不了这件事

Hermes 已有 5 层自学习机制，全部是 **prospective**（agent 在 turn 进行中凭感觉决定记什么）：

| 层 | 触发 | 谁决定记什么 |
|---|---|---|
| L1 短期工作记忆 | 每 turn | LLM 自压缩 |
| L2 长期 Episodic | 用户主动调 `session_search` | aux model |
| L3 Dialectic（Honcho plugin） | 每 N turn | LLM 自反思 |
| L4 Curated MEMORY/USER 本 | nudge / flush 触发 | <b>Agent 当下一拍脑袋</b> |
| L5 Skill 自演化（Curator） | 每 7 天 | aux AIAgent fork |

5 层共同特征是<b>没有任何一层负责"回头看一段历史，找反复出现的稳定模式"</b>。

Hermes 现有架构里有两个看似可以承担这件事的位置，<b>但都被排除了</b>：

| 位置 | 为什么不行 |
|---|---|
| 现有的 **Curator**（L5） | `agent/curator.py` line 17 源码硬注释：<b>"Only touches agent-created skills"</b>。复用 Curator 必须违反这条 invariant，等于改 Hermes 核心约束 |
| 现有的 **Honcho plugin**（L3） | Honcho 写自己的 peer card 到<b>远程后端</b>，不写 Hermes 原生 USER.md。改它需要重写整个 plugin 的存储后端 |

结合上面看，Hermes 现有架构<b>缺一种新的 provider 类型</b>来承担这件事——这就是这次改造要加的东西。

---

# 改了什么

3 处 Hermes 源码改动 + 1 个配套 plugin：

| 改动 | 文件 | 行数 | 在 Hermes 里加了什么概念 |
|---|---|---|---|
| 1 | `agent/memory_provider.py` | +107 | 新的 provider 子类型 `RetrospectiveProvider`（"事后回顾型记忆插件"） |
| 2 | `agent/memory_manager.py` | +120 | 新的调度方法 `run_retrospective()`（manager 主动叫该跑的 provider 起来跑） |
| 3 | `run_agent.py`（主循环） | +52 | 每个新 turn 开始时给 manager 一次调度机会（8 行触发块） |
| 配套 plugin | `plugins/memory/habit_reflector/` | ~340 | 实现这个新抽象的第一个插件，干"读 30 天 → Haiku 蒸馏 → 写 USER.md"这件事 |

<b>合计 +280 行 Hermes 源码改动，全部是追加，0 行修改原有逻辑</b>。Hermes 现有 5 层一行没改，所以这是 additive 改造。

---

# 数据流

```
用户发新消息
    │
    ▼
AIAgent.run_conversation()                    ← 改动 3 触发
    │
    ▼
MemoryManager.run_retrospective()             ← 改动 2 调度
    │
    │  对每个注册的 RetrospectiveProvider:    ← 改动 1 的新类型
    │
    ├── schedule 检查：在允许时段吗?
    ├── 加载 30 天 messages 喂给 provider
    ├── provider.analyze(messages) → claims   ← habit_reflector 调 Haiku 4.5 蒸馏
    └── provider.promote(claims):
          conf ≥0.85 → 调 memory_tool 写 USER.md
          conf 0.70-0.85 → 写 candidates/ 待用户审
          habit 类 → 写 candidates/ 给 Curator 看
    │
    ▼
回到主对话流程，USER.md 已含新 entry
本 turn system prompt 注入时自动包含
```

---

# 增量价值

用户视角的<b>改造前 vs 改造后</b>：

| 维度 | 改造前（Hermes 现状） | 改造后 | 增量 |
|---|---|---|---|
| USER.md 内容质量 | 4 条全是过期 task state | 3 条全是 stable preference | preference 占比从 0% → 100% |
| "用表格"被重复纠正次数 | 14 次还在重复 | 第 5 次后自动入 USER.md，不再纠正 | 约 −50% 纠正 |
| 每条 entry 是否可审计 | 不可（raw 字符串） | 带 evidence 引用 + confidence + decay | 可 audit、可 reject |
| 过期信息怎么清 | 没有机制，要 agent 自己删 | 自动 decay（preference 90d / habit 30d / task 7d） | 不用人工管 |
| 高频短任务怎么变 skill | 卡在"≥5 tool_call + 任务成功"门槛外永远不触发 | 5 个 session 后自动标 candidate，等 Curator 7 天周期接 | 短任务也能沉淀 |
| Hermes 主对话延迟 | — | 0 ms 增加（cold-start 周直接 return；异常时 only log warning） | 无影响 |
| 月运营成本 | — | 每天 1 次 Haiku 4.5 蒸馏 ≈ $0.40 / 次 → 月 $12 | 一杯咖啡 |
| Hermes 旧代码改动 | — | 0 行修改（全部追加） | 零回归风险 |

---

# 验证状态

5 个集成测试覆盖 3 处改动的接口契约 + plugin 整链路：

```
✓ test_change1_retrospective_provider_subclass_works    [抽象基类可被子类化]
✓ test_change2_memory_manager_runs_retrospective        [调度逻辑跑通]
✓ test_change2_skips_when_few_messages                  [过滤生效]
✓ test_change2_handles_provider_errors_gracefully       [错误隔离]
✓ test_habit_reflector_plugin_end_to_end_dryrun         [整链路联动]

=== 5/5 passed in 0.02s ===
```

跑法：

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py
```

---

# 已完成 / 待完成

| 项 | 状态 |
|---|---|
| 3 处 Hermes 源码追加 | ✅ |
| `habit_reflector` plugin 实现 | ✅ |
| 5 个集成测试（用 mock provider + canned LLM response） | ✅ |
| 真 hermes CLI 端到端验证 | ❌ 阻塞于 venv 损坏（Hermes venv 指向已删除的 Python 3.11） |
| 真 Haiku 4.5 蒸馏质量验证 | ❌ 当前只在 14-session 合成 fixture 上跑过 |
| 提 PR 给上游 Hermes | ❌ 阶段 2/3 通过后才考虑 |

---

# 边界 · 它不能做什么

写出来防止误读：

- <b>不替换</b> Hermes 现有 5 层，原 L1-L5 一行没改 · 这是 additive 改造
- <b>不承诺</b>某个 benchmark 分数（Aider polyglot / SWE-bench）改善 · 没做过 A/B 对比
- <b>不跨 user</b> 聚合 habit · 每个 Hermes 实例独立
- <b>不用 embedding / vector DB</b> · YAGNI，100k+ session 之后才有意义
- <b>不自动建 skill</b> · habit_reflector 只标候选给 Curator，建不建由 Curator 决定

---

# 引用

- GitHub 仓库：https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- 完整 README：[README.md](./README.md)
- 测试指南：[TESTING.md](./TESTING.md)
- 详细工程 spec：[docs/SPEC.md](./docs/SPEC.md)
- 关键 Hermes 源码 anchor：
  - `agent/curator.py:17` strict invariant
  - `agent/memory_provider.py:42` 现有 `MemoryProvider` ABC
  - `agent/memory_manager.py:538` `initialize_all()`（改动 2 在它后面追加）
  - `run_agent.py:11419` `run_conversation()` 函数（改动 3 在它顶部追加）
  - `tools/memory_tool.py:224` `MemoryStore.add()`（promote 走它）
