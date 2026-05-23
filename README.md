# Hermes · Episodic + Actionable Habit Memory

**对 Hermes Agent 记忆层做的一次内核改造 · 把 "全量记录" 和 "行为修改" 拆成两个独立 plugin · 加 fitness firewall + 人审 gate.**

> 一句话: 让 Hermes "记住" 这件事不再等于 "偷偷改自己". 记录是 default-on, 改 USER.md 要过两道闸.

---

## 为什么要改

我做过一版 RetrospectiveProvider · 每晚自动蒸馏过去 30 天对话 · 把高置信度偏好直接写进 USER.md. 跑通了集成测试. 然后停下来想了想 — 这条路有<b>两个底层缺陷</b>:

### 11.1 学习不是万能

GEPA / Decagon-style 自我蒸馏的最佳情况是: 重复任务上越来越快.
最坏情况是: 蒸馏出的"经验"在新领域是误导. 把一个特定场景的偏好提升成全局 rule, 在不该用的地方用了, 反而比没有这条 rule 更差.

> 自动学到的 habit 只在"分布内"有正向价值. 一旦用户的下一个任务跨了 domain · 这条 habit 不只是"无效" · 是<b>负向</b>.

### 11.2 行为漂移 (Behavioral Drift)

更危险的是: agent 每晚 silent self-modify · USER.md 在用户不知情的情况下被改写. 一周后用户感觉"它好像不太一样了" · 但已经没法定位是哪条改动 / 什么时候改的 / 怎么 revert.

> Anthropic 自己在 Petri 系列 alignment 研究里反复警告这种模式 — 一个无 audit / 无 revert 的 self-modify loop · 长期看不收敛到对齐, 收敛到 reward hacking.

---

## 怎么改 · 拆成两层 + 三道闸

```
                    ┌──────────────────────────────────────┐
                    │              一次 turn               │
                    └─────────────┬────────────────────────┘
                                  │
              ┌───────────────────┴───────────────────┐
              │                                       │
              ▼                                       ▼
   ┌─────────────────────┐                ┌──────────────────────┐
   │  Layer A · default  │                │  Layer B · 显式启用  │
   │  EpisodicProvider   │                │ ActionableHabit-     │
   │                     │                │ Provider             │
   │  · 全量 append      │                │                      │
   │  · 不蒸馏           │                │  ① fitness_test      │
   │  · 不改 USER.md     │                │       ↓ pass?        │
   │  · 不注入 prompt    │                │  ② await_user_       │
   │                     │                │       approval       │
   │  → 价值: 可靠 recall│                │       ↓ approve?     │
   └─────────────────────┘                │  ③ promote +         │
                                          │       audit log      │
                                          │  → 可一键 revert     │
                                          └──────────────────────┘
```

| | Layer A · `episodic_store` | Layer B · `habit_promoter` |
|---|---|---|
| 默认状态 | <b>enabled · default-on</b> | <b>disabled · 显式 true 才开</b> |
| 改 behavior | 不改 | 改 (写 USER.md) |
| LLM 调用 | 0 / turn | ~1 / day |
| 写盘 | 每 turn append JSONL | 仅 promote 时写 USER.md |
| 失败的代价 | 多一行没用的 episode | 错误 habit 漏过 fitness → 行为漂移 |
| 防护机制 | — | fitness A/B → 人审 → audit + revert |

### Layer A 解决了 11.1

我们不再假装 "agent 能学到东西". Layer A 只是<b>可靠的 episodic recall</b>:

- 用户问 "上周我们聊的那个 SWE-bench 方法论是什么" · agent 调 `recall_episode` 找到原话
- 不蒸馏 · 不抽象成 rule · 不替用户做泛化决策

价值不在"学得快" · 在"找得到". 在 GEPA 的最佳情况下还能学到东西时, Layer B 才被允许介入.

#### 关于信噪比的设计选择

"全量记录" 听起来粗暴. 但写入时做价值判断有一个根本问题: <b>当下 LLM 觉得"无聊"的 turn 三个月后可能恰好是用户要找的那个</b>. 把价值判断推到 retrieve 时 (用 top_k + ranking) 比推到 record 时安全.

代价是磁盘. 一个用户全天高频聊天写满一天大概 < 5MB JSONL · 一年 < 2GB. 接受这个代价.

### Layer B 解决了 11.2

行为漂移问题的根因不是"蒸馏出的 rule 错了", 是"<b>没人 review 就生效了</b>". 拆三道闸:

1. **fitness_test** — A/B 跑 ≥ 8 个 sample query · B 必须胜过 A (default `win_rate ≥ 0.65`) · 不达标的 candidate <b>不进入用户视野</b>, 直接 reject + 写黑名单
2. **await_user_approval** — 过 fitness 的 candidate 写到 `~/.hermes/habit_memory/pending/<id>.json` · 用户用 `approve_habit <id>` / `reject_habit <id>` 决定. <b>不动 USER.md</b>
3. **promote** — 用户批准了才通过 Hermes 官方 memory API 写 USER.md · 同时记 audit.log · 永远可 revert

任何 silent self-modify 路径都被这条 pipeline 堵住.

---

## 改了什么文件

Hermes 原代码<b>一行没动</b> · 全部 additive:

| 文件 | 增量 | 干什么 |
|---|---|---|
| `agent/memory_provider.py` | +208 行 | 加 `Episode` / `HabitCandidate` / `FitnessReport` / `Schedule` dataclass · `EpisodicProvider` ABC · `ActionableHabitProvider` ABC |
| `agent/memory_manager.py` | +172 行 | 加 `record_episode` / `retrieve_episodes` / `propose_habits` / `run_habit_promotion_pipeline` |
| `plugins/memory/episodic_store/` | 新 plugin · ~200 行 | Layer A 实现 · JSONL-backed |
| `plugins/memory/habit_promoter/` | 新 plugin · ~280 行 | Layer B 实现 · fitness + 人审 + audit |
| `tests/test_pathB_integration.py` | 7 个测试 | 覆盖两层 + fitness gate + 人审 gate + 全链路 |

---

## 测试状态

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py

# 输出:
#   ✓ test_episodic_provider_subclass_works               [Layer A 抽象能用]
#   ✓ test_actionable_habit_provider_subclass_works       [Layer B 抽象能用]
#   ✓ test_episodic_store_plugin_record_retrieve          [Layer A 插件链路]
#   ✓ test_habit_promoter_fitness_gate_rejects_weak       [fitness 防漂移]
#   ✓ test_habit_promoter_user_approval_gate_blocks       [人审防漂移]
#   ✓ test_habit_promoter_full_pipeline_promotes_when_ok  [全链路绿灯]
#   ✓ test_propose_habit_distills_candidates              [蒸馏链路]
# === 7/7 passed ===
```

关键测试场景:

- **Test 4 · fitness gate 防漂移**: 构造一个 win_rate=0.3 的 weak candidate · pipeline 必须 reject · USER.md 必须 0 写入
- **Test 5 · 人审 gate 防漂移**: fitness 过了但用户拒绝 · 同样 0 写入
- **Test 6 · 全链路绿灯**: fitness pass + 用户批准 → 真写 USER.md + audit.log 记录

---

## 什么<b>没有</b>验证

诚实标注:

- 真接到 Hermes CLI 端到端跑 (本机 Hermes venv 损坏阻塞 · 测试用独立 python3 + symlinks)
- 真 Haiku 4.5 蒸馏的 candidate 质量 (当前 dry_run 用 canned response)
- 真生产的 LLM-as-judge fitness scoring 是否稳定 (placeholder 用 `random.random() < raw_confidence`)
- USER.md 长期写入 / revert 后的内容一致性 (没跑过 ≥ 30 天)

如果要上生产, 这三块都需要 label 一批真实样本回归.

---

## 与 Hermes 原生 5 层记忆的关系

Hermes 已有的 5 种记忆方式 + 我加的这两层:

| 现有层 | 干什么 | 与本改造关系 |
|---|---|---|
| Auto-compress | 每 turn 压缩历史 | 不冲突 · Episodic 在 compress 之前 record |
| Search history | 用户搜过往聊天 | Episodic 更结构化 · 但不替代 |
| Honcho self-reflect | LLM 几句话自反思 | 不冲突 · 它写 peer card · 不写 USER.md |
| Periodic "memory check" | 每 10 句话提醒 agent 记 | 跟 Layer B 部分重叠 · 但<b>没有 fitness gate</b> |
| Skill Curator | 每 7 天整理 skills | 只动 skills/ · 不动 USER.md · 不冲突 |

最大的差异: <b>没有一个原生层有 fitness gate + 人审 gate</b>. 这是本改造引入的新约束.

---

## 如果你看完只想记 3 件事

1. <b>记录 ≠ 学习</b> · 把 episodic memory 跟 behavior modification 拆开 · 是 Hermes 现在缺的关键抽象
2. <b>fitness firewall</b> 是 11.2 行为漂移的解 · A/B 跑不过的 candidate 不让用户看到 · 用户看到的都跑过 fitness
3. <b>显式 promote</b> · 任何写 USER.md 的路径必须经过人审 · 不能 silent self-modify · 永远可 revert + audit

---

## 边界声明

- 这是<b>笔试 demo</b> · 不是 production-ready · LLM-as-judge 那部分是 placeholder
- 适用前提: 用户愿意每周花 ~5 分钟 review 几个 candidate · 不愿意 review 的话 Layer B 直接关掉, Layer A 单跑也有价值
- 不适用场景: 高频任务流 (agent 一天蒸馏出 50 条 candidate) · 这种规模需要分桶 + auto-tiering · 当前 pipeline 没做

---

## 相关代码 / 仓库

- GitHub: https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- 测试: `tests/test_pathB_integration.py`
- 变更详单: `CHANGES.md`
