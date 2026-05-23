# 改造说明 · 凝练版

> 比 README 短一个量级。重点说清楚<b>改了什么、为什么这么改、边界在哪</b>。
> 5 分钟读完。

---

## 1. 一句话定位

给 Hermes 内核加一个抽象基类 `RetrospectiveProvider`，让"事后回顾型记忆插件"成为 Hermes 调度系统能识别的<b>正式插件类型</b>，而不是必须靠外挂 cron 干这件事。

---

## 2. 改造源头 · 我观察到的事实

直接从 [`NousResearch/hermes-agent` 源码](https://github.com/NousResearch/hermes-agent) 里读出来的，不是推断：

- `agent/memory_provider.py` 中的 `MemoryProvider` 抽象方法 `prefetch / sync_turn / on_turn_start` 全部是<b>turn-level 同步触发</b>的接口。没有任何 batch / scheduled / retrospective 入口。
- `agent/memory_manager.py` 的 `MemoryManager` 是被动响应者——所有方法都是 agent 调它，没有"manager 自己主动调度 provider"的方法。
- `agent/curator.py` 第 17 行严格注释：<b>"Only touches agent-created skills"</b>。Curator 不动 USER.md / MEMORY.md。
- `tools/memory_tool.py` 的 `MemoryStore.add()` 函数（line 224 起）是<b>纯字符串 entry，无 schema 字段</b>（没 confidence、没 decay、没 evidence）。
- 6 个已存在的 memory plugin（`plugins/memory/{honcho, retaindb, openviking, byterover, holographic, supermemory}`）<b>全都实现 prospective `MemoryProvider` 接口</b>，没有一个有 retrospective 通道。

结合上面看，Hermes 现有架构里<b>没有任何位置承担"回头看一段历史"这件事</b>。要做这件事必须从外面挂——这就是路 A 那种外挂方案的根因。

---

## 3. 改了什么 · 一张表

| 改动 | Hermes 原文件 | 行数变化 | 引入什么 |
|---|---|---|---|
| 改动 1 | `agent/memory_provider.py` | 279 → 386（+107，全在文件尾追加） | `RetrospectiveProvider` ABC + `Schedule` / `Claim` / `PromotionResult` dataclass |
| 改动 2 | `agent/memory_manager.py` | 555 → 675（+120，在 `initialize_all` 后追加） | `run_retrospective()` 方法 + `get_retrospective_providers()` 查询方法 |
| 改动 3 | `run_agent.py` | 15469 → 15521（+52，在 `run_conversation` 函数内 + 类底部） | 一个 try/except 触发块 + `_load_messages_for_retrospective` loader 方法 |
| 新加 | `plugins/memory/habit_reflector/` | 新目录 ~340 行 | 实现 `RetrospectiveProvider` 的官方插件，演示这套接口怎么用 |

合计 280 行 Hermes 源码改动 + 340 行 plugin。<b>0 行修改任何旧逻辑</b>——所有改动都是文件末尾追加或函数内的 try/except 包围块。

---

## 4. 数据流 · 一张图

```
用户发新消息
    │
    ▼
AIAgent.run_conversation()
    │
    │  改动 3 触发
    ▼
MemoryManager.run_retrospective(trigger="conversation_start", message_loader=...)
    │
    │  改动 2 调度
    ▼
找所有 RetrospectiveProvider                    ←  改动 1 加的抽象
  对每个：
    ├─ schedule().allowed_hour_range 在范围? 否 → skip
    ├─ message_loader(window_days=30) 拉历史   ←  loader 由 AIAgent 注入
    │   消息 <10 条 → skip
    ├─ provider.analyze(messages) 返回 List[Claim]
    └─ provider.promote(claims) 返回 PromotionResult
         │
         ▼
       走 manager.handle_tool_call("memory_write", ...)
       写进 USER.md / MEMORY.md
       (路径跟 Hermes 原生 memory_tool 一样，不绕过)
```

---

## 5. 它解决的具体问题

下面 3 条只讲 Hermes 现在<b>确切跑不出来的</b>结果，加上这套抽象后<b>能跑出来的</b>结果。不放空话：

| 现状（Hermes 当下） | 加入后 |
|---|---|
| `USER.md` 只能在 `nudge`（默认每 10 turn）或 `flush`（session 退出）时被 agent 当下决定写入 | 多一条入口：scheduled batch · 由 RetrospectiveProvider 子类决定 |
| 任何 plugin 想做"回顾过去 N 天"必须自己起 cron，绕过 Hermes 内部 API | 通过 `run_retrospective` 走官方调度；plugin 在 manager 注册表里被识别 |
| `MemoryProvider.sync_turn` 是<b>每 turn 一次</b>的接口，跑跨 session 的 batch 分析等于重做架构 | `analyze(messages, window_days)` 显式接 batch 一段历史 |
| Curator 7 天周期决定 keep/patch/archive skill，但<b>没机制让外部建议候选 skill</b> | 任何 RetrospectiveProvider 可写 `candidates/*.candidate` 给 Curator 看 |

---

## 6. 配套 plugin · habit_reflector 做了什么

```
~/.hermes/state.db (Hermes 原 SQLite)
    │
    │  loader 拉 30 天 messages
    ▼
HabitReflector.analyze(messages)
    │  调 Haiku 4.5 蒸馏
    │  本地公式重算 confidence（不信模型自评）
    │  classification ∈ {preference, habit, task, constraint}
    │  evidence ≥3 条 session/turn 引用
    ▼
List[Claim]
    │
    ▼
HabitReflector.promote(claims)
    │  conf ≥0.85: manager.handle_tool_call("memory_write", target="user")
    │  conf 0.70-0.85: 写 candidates/<id>.pending.json
    │  classification=="habit": 额外写 candidates/<name>.candidate
    ▼
Hermes USER.md + candidates/
```

本地 confidence 公式：起步 0.10 + ≥5 次 +0.25 + ≥3 session +0.25 + 最近 7 天 +0.25 + 用户显式确认 +0.25。

---

## 7. 验证状态 · 老实讲

| 验证范围 | 状态 | 边界 |
|---|---|---|
| 抽象基类能正确被子类化 | ✅ 通过测试 | 测试用 mock provider，没用真 plugin |
| `MemoryManager.run_retrospective` 调度逻辑 | ✅ 通过测试 | 用 mock message_loader 喂数据 |
| 错误隔离（provider 抛错不崩 manager） | ✅ 通过测试 | 用故意抛错的 mock provider |
| `habit_reflector` plugin 整链路 | ✅ 通过测试 | 用<b>canned response</b>（硬编码假返回）跑，<b>没调真 Haiku 4.5 API</b> |
| 集成 Hermes 真跑 turn 触发改动 3 | ❌ <b>没在实机验证过</b> | 测试用独立 Python 进程，没真 launch `hermes` CLI |
| 真实 Haiku 4.5 蒸馏质量 | ❌ <b>没验过</b> | 整套 plugin 只在 14-session 合成 fixture 上跑过 |

5/5 集成测试通过，意思是<b>接口契约和数据流</b>都对，但跟真 Hermes / 真 LLM 的兼容性<b>还没在端到端环境验过</b>。

---

## 8. 这个改造的边界 · 它不能做什么

写出来防止误读：

- 它<b>不</b>替换 Hermes 现有 5 层自学习——只新加一层，原 5 层一行没改。
- 它<b>不</b>承诺改善某个具体 benchmark 分数（Aider polyglot / SWE-bench 等）。从未做过 A/B 对比，连合成基线都没建。
- 它<b>不</b>跨 user 聚合 habit——每个 Hermes 实例独立。隐私问题不在本范围。
- 它<b>不</b>用 embedding / vector DB。简单 keyword 蒸馏 + 本地 BM25 检索（如果以后要加搜索）。
- 它依赖 `~/.hermes/state.db` 上有 `messages` 表 + `list_messages_since(cutoff)` 方法。<b>这个 API 我没去 Hermes 源码里逐字核实过</b>——loader 函数里如果方法名错了得调一下。

---

## 9. 适用 vs 不适用

| 用得上 | 用不上 |
|---|---|
| 你想给 Hermes 加一种新的"回顾分析"插件（比如：每周分析用户什么时段最 productive） | 你只想做 prompt 优化 |
| 你想让 USER.md 不再被 task state 占满 | 你有完全独立的 memory backend（Honcho / Mem0 等），且不接 Hermes 原 USER.md |
| 你要给上游 Hermes 提 PR 加抽象层 | 你只想自己一次性脚本能跑通 |
| 你的 plugin 需要从 manager 注册表里查询/被其他 plugin 引用 | 单文件、不需要被识别的脚本 |

---

## 10. 一行命令测试

```bash
cd /Users/项目开发/harness-research/hermes-fork-pathB && \
  export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent" && \
  python3 tests/test_pathB_integration.py
```

预期 `=== 5/5 passed ===`。需要 0 个 API key，~5 秒跑完。

---

## 11. 来源

- 仓库：https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- Hermes 源码引用基于 `~/.hermes/hermes-agent/` （v0.x，2026-05 你本机的版本）
- README 更长版：见 [README.md](./README.md)
- 测试指南：见 [TESTING.md](./TESTING.md)
