# 怎么在本机测试这个 fork 改造

> 3 层测试 · 从最简单（不需要 API key, 5 秒）到最完整（真集成 Hermes）.

---

> ⚠️ **提示**: 如果你的 Hermes venv 是坏的（指向已删除的 Python 3.11), 用<b>系统 python3</b> 即可:
> ```bash
> brew install pyyaml-python || pip3 install pyyaml --break-system-packages
> python3 --version  # 应该 ≥3.10
> ```

---
---

## 📋 你本地的 Hermes 状态

- ✅ 已装 Hermes CLI: `/Users/zane/.local/bin/hermes`
- ✅ Hermes 源码: `~/.hermes/hermes-agent/`
- ✅ Hermes 自带 venv: `~/.hermes/hermes-agent/venv/bin/python`
- ✅ venv 含 anthropic SDK + yaml
- ❌ 没跑过 session (`~/.hermes/sessions/` 是空的, `~/.hermes/state.db` 不存在)
- ❌ 没配 Anthropic API key (config.yaml 用 `anthropic/claude-opus-4.6` 但需要 `ANTHROPIC_API_KEY` 环境变量)

---

## 🟢 Level 1 · Dry-run 测试 (5 秒 · 不需要 API key)

**目的**: 验证 fork 的 3 处改动逻辑正确, 不烧 token

```bash
cd /Users/项目开发/harness-research/hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"
python3 tests/test_pathB_integration.py
```

**预期输出**:

```
  ✓ test_change1_retrospective_provider_subclass_works    [改动 1 抽象能用]
  ✓ test_change2_memory_manager_runs_retrospective        [改动 2 调度能用]
  ✓ test_change2_skips_when_few_messages                  [改动 2 过滤生效]
  ✓ test_change2_handles_provider_errors_gracefully       [改动 2 错误隔离]
  ✓ test_habit_reflector_plugin_end_to_end_dryrun         [整链路联动]

=== 5/5 passed ===
```

**这一层证明了什么**:

- ✅ `RetrospectiveProvider` 抽象正确定义, 能被子类化
- ✅ `MemoryManager.run_retrospective()` 调度逻辑正确 (找 provider, 检查 schedule, 喂 messages, 调 analyze + promote)
- ✅ 错误隔离 (一个 provider 挂掉不影响别的)
- ✅ Habit Reflector plugin 完整链路跑通 (调度 → 分析 → promote → 写 USER.md)

这一层<b>不调真 API</b>, 用的是 plugin 内置的 canned response (硬编码的 3 条 fake claim).

---

## 🟡 Level 2 · 用真 Anthropic API 跑一次 (1 分钟 · 需要 API key · ~$0.001)

**目的**: 让真实的 Haiku 4.5 蒸馏一段消息, 看实际产物.

### 准备

1. 拿你的 Anthropic API key (https://console.anthropic.com/settings/keys)
2. 临时 export 进环境:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### 跑

```bash
cd /Users/项目开发/harness-research/hermes-fork-pathB
export PYTHONPATH="$PWD:$HOME/.hermes/hermes-agent"

python3 <<'PYEOF'
import json, tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 准备一段合成对话喂给 reflector (模拟"用户 14 次都说要表格")
messages = []
for i in range(60):
    sess = f"s_2026_05_{i//4:02d}"
    messages.append({"session_id": sess, "role": "user",
                     "content": f"用表格列出 SWE-bench Pro 方法学" if i%6 == 0 else f"普通消息 {i}",
                     "turn_n": i%4+1, "timestamp": 0})

# 创建 reflector
from agent.memory_manager import MemoryManager
from plugins.memory.habit_reflector import HabitReflector

manager = MemoryManager()
reflector = HabitReflector()
reflector._dry_run = False  # ← 用真 API

# 简单 mock memory_tool 的 add (避免依赖完整 Hermes)
write_log = []
manager.handle_tool_call = lambda n, a, **kw: (write_log.append(a), json.dumps({"success": True}))[1]
manager.add_provider(reflector)

with tempfile.TemporaryDirectory() as tmp:
    # 跳过 cold-start
    reflector.initialize("test", hermes_home=tmp, memory_manager=manager)
    eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    (Path(tmp)/"habit_memory"/".state.json").write_text(json.dumps({"installed_at": eight_days_ago}))

    print("🚀 调 Haiku 4.5 蒸馏 60 条 messages...")
    result = manager.run_retrospective(
        trigger="test",
        force=True,  # 跳过 allowed_hour_range
        message_loader=lambda window_days=30: messages,
    )

    print(f"\n=== 结果 ===")
    print(f"成功跑的 provider: {len(result['ran'])}")
    print(f"错误: {len(result['errors'])}")
    if result["ran"]:
        info = result["ran"][0]
        print(f"\nHaiku 蒸馏出 {info['claims_total']} 条 claim:")
        print(f"  · promoted_to_usermd: {info['result'].promoted_to_usermd}")
        print(f"  · queued_for_review: {info['result'].queued_for_review}")
        print(f"  · skill_candidates: {info['result'].skill_candidates}")

    if result["errors"]:
        for e in result["errors"]:
            print(f"\n❌ {e['name']} stage={e['stage']}: {e['error']}")

    # 看 USER.md 实际被写了啥
    print(f"\n=== Memory write 调用 (路 B 走 manager.handle_tool_call 而不是直接写文件) ===")
    for w in write_log:
        print(f"  · target={w['target']} content={w['content'][:80]}...")
PYEOF
```

**这一层证明了什么**:

- ✅ 真实 Haiku 4.5 能从消息蒸馏出 claims (不是 canned 数据)
- ✅ 路 B 真的<b>通过 `manager.handle_tool_call`</b> 而不是绕过 Hermes 直接写文件
- ✅ Confidence 本地公式重算 (不信模型自评)

---

## 🔴 Level 3 · 真接进 Hermes (10 分钟 · 改 ~/.hermes/hermes-agent)

**目的**: 真把 fork 改动 apply 到本地 Hermes, 跑 `hermes` CLI 验证 turn 触发.

### Step 3.1 · 备份原文件

```bash
cd /Users/zane/.hermes/hermes-agent
cp agent/memory_provider.py agent/memory_provider.py.orig
cp agent/memory_manager.py agent/memory_manager.py.orig
cp run_agent.py run_agent.py.orig
```

### Step 3.2 · Apply 改动 1 (memory_provider.py)

把 fork 的 RetrospectiveProvider 抽象追加到 Hermes 原文件末尾:

```bash
FORK="/Users/项目开发/harness-research/hermes-fork-pathB"

# 取 fork 文件的 280 行后的所有新增 (RetrospectiveProvider 块)
tail -n +280 "$FORK/agent/memory_provider.py" >> /Users/zane/.hermes/hermes-agent/agent/memory_provider.py
```

### Step 3.3 · Apply 改动 2 (memory_manager.py)

```bash
# fork 在 line 556 之后是新增内容
tail -n +556 "$FORK/agent/memory_manager.py" >> /Users/zane/.hermes/hermes-agent/agent/memory_manager.py
```

### Step 3.4 · Apply 改动 3 (run_agent.py)

这一个最难——不能简单 append, 要插到 `run_conversation()` 函数中. 用 sed 在 `self._restore_primary_runtime()` 后插:

```bash
# 找到那一行的行号
LINE=$(grep -n "self._restore_primary_runtime()" /Users/zane/.hermes/hermes-agent/run_agent.py | head -1 | cut -d: -f1)
echo "改动 3 插入点: line $LINE"

# 在该行之后插入 retrospective trigger
RUNAGENT="/Users/zane/.hermes/hermes-agent/run_agent.py"
sed -i.bak "${LINE}a\\
\\
        try:\\
            if hasattr(self, 'memory_manager') and self.memory_manager is not None:\\
                self.memory_manager.run_retrospective(trigger='conversation_start')\\
        except Exception as e:\\
            logger.warning('retrospective trigger failed: %s', e)\\
" "$RUNAGENT"
```

### Step 3.5 · Copy plugin

```bash
mkdir -p /Users/zane/.hermes/hermes-agent/plugins/memory/habit_reflector
cp "$FORK/plugins/memory/habit_reflector"/*.py \
   "$FORK/plugins/memory/habit_reflector"/*.yaml \
   /Users/zane/.hermes/hermes-agent/plugins/memory/habit_reflector/
```

### Step 3.6 · 启用 plugin

```bash
cat >> /Users/zane/.hermes/config.yaml <<'EOF'

# ★ 路 B · habit_reflector plugin
plugins:
  memory:
    habit_reflector:
      enabled: true
      distiller_model: "claude-haiku-4-5"
      window_days: 30
EOF
```

### Step 3.7 · 准备 API key 启动 Hermes

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
hermes
```

跟它聊几个回合, 看 Hermes 启动日志里有没有出现:

```
[INFO] Memory provider 'habit_reflector' registered (0 tools)
[INFO] retrospective trigger at conversation_start ...
```

如果出现就说明改动 3 生效了 (虽然 cold-start week 1 不会真跑分析, 但触发是工作的).

### Step 3.8 · 还原（不喜欢回滚）

```bash
cd /Users/zane/.hermes/hermes-agent
mv agent/memory_provider.py.orig agent/memory_provider.py
mv agent/memory_manager.py.orig agent/memory_manager.py
mv run_agent.py.orig run_agent.py
rm run_agent.py.bak
rm -rf plugins/memory/habit_reflector
# 同时手动编辑 ~/.hermes/config.yaml 删掉 plugins.memory.habit_reflector 那一节
```

---

## 🎯 我建议你怎么测

| 阶段 | 推荐 |
|---|---|
| **第一次看** | <b>跑 Level 1</b> · 5 秒看完 · 证明改动逻辑对 |
| **想看真东西** | <b>跑 Level 2</b> · 给我 API key 或自己跑 · 看 Haiku 真蒸馏什么 |
| **完全集成** | <b>Level 3</b> · 1-2 小时 · 改 Hermes 真跑 · 需要谨慎 (改完记得 3.8 还原) |

---

## ⚠️ Level 3 风险提示

- ✅ Level 3 是<b>可以做的</b> · 改动都是追加, 不破坏 Hermes 原逻辑
- ⚠️ 但要<b>先备份</b> (Step 3.1 已经包括)
- ⚠️ 如果 Hermes 启动报错 → 跑 Step 3.8 还原, 然后告诉我什么错
- ⚠️ Level 3 我没在你本机实际跑过 (这个测试需要你本人执行)
