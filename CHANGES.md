# Hermes Fork · 改造详单

## 一句话讲清楚核心价值

**Hermes 之前没有一种"先想清楚要不要改, 再改"的记忆机制. 我把"全量记录"和"修改 USER.md"拆成两层 · 中间加 fitness firewall + 人审 gate · 让 Hermes 长期不漂移.**

---

## 为什么不是一层而是两层

### 11.1 学习不是万能

GEPA / 单层 RetrospectiveProvider 的最佳情况: 重复任务上越来越快.
最坏情况: 跨域时蒸馏出的 rule 反而是负向干扰.

→ 拆出 Layer A · 它<b>不学习</b> · 不试图泛化 · 只做可靠的 episodic recall.
→ 这样, 即使 Layer B 蒸馏给错了, 用户依然有 ground truth 可以查.

### 11.2 行为漂移

单层方案每晚 silent self-modify USER.md · 用户无法定位是哪条改动 / 何时改的 / 怎么 revert.

→ 拆出 Layer B · 任何 USER.md 改动必须过<b>三道闸</b>:
  ① `fitness_test` A/B 评估 → ② `await_user_approval` 人审 → ③ `promote` + audit log + 可 revert

---

## 改造范围

Hermes 原代码 0 修改 · 全部 additive:

| 文件 | 性质 | 增量 |
|---|---|---|
| `agent/memory_provider.py` | 追加 | +208 行 · 4 个 dataclass + 2 个新 ABC |
| `agent/memory_manager.py` | 追加 | +172 行 · 双层调度 + 三道闸 pipeline |
| `plugins/memory/episodic_store/` | 全新 | Layer A 实现 |
| `plugins/memory/habit_promoter/` | 全新 | Layer B 实现 |
| `tests/test_pathB_integration.py` | 重写 | 7 个测试覆盖关键不变量 |

---

## Layer A · `episodic_store`

| | |
|---|---|
| 默认 | <b>enabled · default-on</b> |
| 干什么 | 每 turn append 一行 JSONL 到 `~/.hermes/episodes/<session>.jsonl` |
| 不干什么 | 不蒸馏 · 不改 USER.md · 不注入 system prompt |
| 暴露 tool | `recall_episode(query, top_k, session_id?)` |
| LLM 调用 | 0 · 写入 < 5ms · pure I/O |
| 月成本 | $0 |

#### 信噪比策略

写入时<b>不</b>做价值判断. 当下"无聊"的 turn 三个月后可能就是用户要找的那个.
价值判断推到 retrieve 时 (top_k + ranking, user_pinned 强加分, 时间 tiebreak).

#### user_pinned 机制

用户可以显式 `pin <episode_id>` · 这一条永远在检索结果顶部. 用来对抗 ranking 误判.

---

## Layer B · `habit_promoter`

| | |
|---|---|
| 默认 | <b>disabled · 必须显式 enabled: true</b> |
| 干什么 | 凌晨 2-5 点蒸馏过去 30 天 episodes → propose candidates → 跑 fitness → 等人审 |
| 才能写 USER.md 的条件 | ① fitness verdict == "pass" AND ② user_approved == True |
| LLM 调用 | ~1/day (distiller) + ~8/day (fitness A/B 跑 sample query) |
| 月成本 | ~$12 (Haiku 4.5 + Sonnet judge) |

#### 三道闸

```python
# manager.run_habit_promotion_pipeline()
report = manager.run_habit_promotion_pipeline(
    candidate, provider=habit_promoter,
    sample_queries=[...],  # ≥ 8 个 sample query
    require_user_approval=True,  # 生产必须 True
)

# 任何一道闸 fail → 不写 USER.md · 记 audit log:
# - rejected_fitness_fail
# - rejected_fitness_neutral  (默认 fail · 可配置 0.45 ≤ win_rate < 0.65 时 ask)
# - rejected_user_declined
# - rejected_promote_error

# 全过 → promoted · USER.md 写入 + audit log + pending/ 文件移到 promoted/
```

#### audit log 样例

```json
{"ts": "2026-05-23T02:14:08Z", "action": "pending", "candidate_id": "hc_a1b2c3d4", "claim": "用户强偏好结构化输出"}
{"ts": "2026-05-23T08:42:11Z", "action": "promoted", "candidate_id": "hc_a1b2c3d4", "claim": "用户强偏好结构化输出"}
{"ts": "2026-06-15T19:33:02Z", "action": "reverted", "candidate_id": "hc_a1b2c3d4"}
```

---

## 验证状态

**已验证** (7/7 测试通过):

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py
```

每个测试覆盖一个关键不变量:

| Test | 不变量 |
|---|---|
| Test 1 | Layer A 抽象正确可子类化 |
| Test 2 | Layer B 抽象正确可子类化 |
| Test 3 | episodic_store 写入 → 检索 → pin 全链路 |
| Test 4 | <b>fitness 不达标的 candidate 不能写 USER.md</b> |
| Test 5 | <b>用户没批准的 candidate 不能写 USER.md</b> |
| Test 6 | fitness + 人审都过, 写 USER.md + audit log |
| Test 7 | distiller 蒸馏链路 (dry_run canned) |

**未验证** (诚实标注):

- 真接进 Hermes CLI 端到端跑 (本机 venv 损坏)
- 真 Haiku 4.5 蒸馏质量在真实样本上 label
- LLM-as-judge fitness scoring 稳定性 (当前 placeholder)
- USER.md 在长期 promote / revert 后的内容一致性

---

## 价值清单

| 项 | 价值 |
|---|---|
| Layer A · 全量 episodic | 即使 Layer B 错了, 用户依然有 ground truth 可查 |
| Layer A 默认开 | 0 风险副作用 (只写 episodes/) · 不改 behavior |
| Layer B 默认关 | 用户不知情时不可能发生 self-modify |
| fitness gate | 不达标的 candidate 不进入用户视野 · 节省用户 review 时间 |
| 人审 gate | 任何 USER.md 写入用户都签过字 · 漂移问题被堵住 |
| audit + revert | 所有改动可回溯 · 一键回到任意时间点 |
| Hermes 原代码 | 0 行修改 · 0 回归风险 |
| 提 PR 给上游 | 可行 · 是 additive 抽象 |

---

## 仓库 + 配套文档

- **代码 + 完整 README**: https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- **测试指南**: [TESTING.md](./TESTING.md)
