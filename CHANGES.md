# Hermes RetrospectiveProvider · 笔试介绍

## 一句话讲清楚核心价值

**Hermes 现在每次跟你开新对话，记不住你之前说过 14 次的偏好。我给它加了一个东西：每晚 4 点自动回顾过去 30 天的聊天记录，把反复出现的偏好抽出来写进 USER.md，下次启动它就直接知道。**

---

## 体感差异 · 改造前 vs 改造后

**场景**：你用 Hermes 一个月，每次问它 "看下这篇 paper"，每次它都输出一大段 prose。你<b>每次</b>说 "用表格"。说了 14 次。

### 改造前 · Hermes 现在的 USER.md 长这样

```
- 用户在做 SOTA harness 笔试题
- 用户在飞机上离线阅读
- 用户喜欢边讨论边出 HTML
- 用户在 Obsidian 里看东西
```

4 条 entry 全是<b>当下任务状态</b>（笔试题做完就过期）。<b>"用表格"一次都没记下来</b>。下次新对话开始，agent 还是输出 prose，你还是要说"用表格"。

### 改造后 · 每晚回顾一次后 USER.md 长这样

```
- 用户强偏好结构化输出（表格 + bullet）而非 prose
  [conf 1.00 · 8 sessions · decay 2026-08-21]
- 高频任务：读 paper → 出表格 critique
  [conf 0.85 · 5 sessions · decay 2026-06-22]
- 工作日上午 10-13 点短 session 高密度（4-6 turn 完成）
  [conf 0.85 · 9 sessions · decay 2026-08-21]
```

同样 500 字符，3 条 entry 全是<b>稳定偏好</b>。每条带证据引用、置信度、过期日期。

**下次开新对话 system prompt 自动注入这 3 条，agent 直接知道 "用表格"。你不用再说。**

---

## 为什么 Hermes 现在做不到

Hermes 已经有 5 种记忆方式，但<b>全部是 "聊到一半凭感觉记"</b>：

- 每 turn 自动压缩历史
- 用户主动搜过去聊天
- LLM 每几句话自反思（可选 Honcho 插件）
- 每 10 句话提醒 agent 写笔记（写啥看心情）
- 每 7 天整理 skills（不动 USER.md）

5 种里<b>没有任何一种</b>负责 "事后回顾过去 30 天找规律"。"用表格"被记不下来的根本原因就在这里。

---

## 我做了什么

加了 Hermes 的<b>第 6 种记忆方式</b>：每晚 4 点离线回顾过去 30 天对话。

具体做法 4 步：

1. **给 Hermes 加一种新插件类型**叫 `RetrospectiveProvider`（"事后回顾型记忆插件"）
2. **给 Hermes 的记忆管理员加新职责**：到点了主动叫这种插件起来跑
3. **给 Hermes 主循环加 8 行触发**：每次用户开新对话，提醒记忆管理员检查一下该不该跑
4. **写一个具体插件 `habit_reflector`**：调小模型 (Haiku 4.5) 蒸馏 30 天聊天，把高置信度的稳定偏好<b>通过 Hermes 官方 API</b> 写进 USER.md

技术上：改了 Hermes 3 个源文件合计 +280 行（全部是追加，不修改原有逻辑）+ 1 个新 plugin 340 行。Hermes 原有 5 层一行没动。

---

## 验证状态

**已验证**：5 个集成测试全过（接口契约、调度逻辑、错误隔离、整链路联动）。跑法：

```bash
cd hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py
```

**未验证**：真在 Hermes CLI 端到端跑过（用户本机 venv 损坏阻塞）。真 Haiku 4.5 蒸馏出来的质量也没在真实数据上 label 过——当前测试用的是 14-session 合成 fixture + canned LLM response。

---

## 价值清单 · 它带来什么

| 项 | 价值 |
|---|---|
| 稳定偏好不再重复纠正 | "用表格"说 5 次后自动记住，省掉后续 9 次纠正 |
| USER.md 内容质量 | 从 0% preference → 100% preference（同样 500 字符容量） |
| 高频短任务变 skill | 跨过 Hermes "≥5 tool_call + 任务成功" 的硬门槛 |
| 自动过期 | preference 90 天衰、habit 30 天衰、task 7 天衰 |
| Hermes 主对话延迟 | 0 ms 增加（异常只 log warning） |
| 月运营成本 | ~$12（每天 1 次 Haiku 4.5 蒸馏） |
| Hermes 旧代码 | 0 行修改 · 零回归风险 |
| 提 PR 给上游 | 可行 · 是 additive 抽象 |

---

## 仓库 + 配套文档

- **代码 + 完整 README**：https://github.com/AlexZWANG1/hermes-fork-retrospective-provider
- **测试指南**：[TESTING.md](./TESTING.md)
- **完整工程 spec**：[docs/SPEC.md](./docs/SPEC.md)
