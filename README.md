# Harness² × Super-agent：OpenCode / Codex PoC

通过同一套任务快照、Harness² 优化流程和独立验收入口，选择 OpenCode 或 Codex 执行任务。Super-agent Worker 可以通过已有的 `bash` 工具调用它。本项目是 CLI PoC，没有 HTTP 服务，也不会自动发布候选配置或代码。

```text
普通任务 / Super-agent Worker ──→ Runner ──→ OpenCode 或 Codex
                                  ↑
                    官方 Harness² 优化流程
                    Solver 和 Editor 均使用所选执行器
                                  ↓
                    候选配置 → 独立验收 → Worker 复测
```

## 当前支持

| 组件 | 实现与验证状态 |
| --- | --- |
| Harness² | 固定官方 commit，复用原有 `k=1` parallel 优化流程 |
| OpenCode | 原生 CLI、JSONL 轨迹、产物、用量、超时与取消；v0.1 已通过真实 CLI＋回环模拟模型测试 |
| Codex | 新增 `codex exec --json` 适配；普通任务、Worker、Solver、Editor 共用；本次验证使用 CLI 协议替身 |
| Super-agent | 提供 Ticket Worker 调用入口；真实 DSH Host 生命周期尚未联调 |
| 真实模型质量 | 尚未验证；测试通过不代表优化提升了模型效果 |

两个执行器都优化同样的四个文本文件，运行时合并到工作区的 `AGENTS.md`：`systemprompt.md`、`guardrails.md`、`LongTermMEMORY.md`、`tool_descriptions/tool_guidance.md`。这四个名字是本 PoC 的配置格式，Codex 不会分别加载它们。当前没有开放插件、脚本、工具权限或子 Agent 配置优化。

## 在 GitHub 上验证

把本目录的内容作为仓库根目录上传，包含 `.github/workflows/test.yml`；不要把外层 `outputs/` 一并作为根目录。无需配置 Secret，推送或创建 PR 后自动运行：

1. `protocol-and-recursion`：安装 Python 依赖，执行全部测试，再分别运行两种执行器的确定性演示。无需安装 Agent CLI，也不会调用模型。
2. `native-codex-protocol`：只在 GitHub 临时 Runner 中安装锁定版本的 CLI，启动真实 Codex，连接 Runner 内的回环 Responses 模拟服务，检查 Harness 加载、工作目录、原生终态和 Token。无外部模型请求、无真实 API Key，结果作为 workflow artifact 保存。

**本次交付已执行第一类测试；第二个 job 已提供，尚未在 GitHub 上运行。**真实 Codex CLI 的验证结果以该 job 和产物为准。生产凭据不要添加到这两个测试 job。

## 在独立开发环境运行

以下命令用于 GitHub Codespaces、临时开发机或其他自行准备的干净 POSIX 环境；不需要全局安装，也不依赖作者机器的目录。Python 需为 3.12 或 3.13。只有运行真实 Agent 时才需要 Node.js/npm。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

# 两个 demo 都是测试替身，不安装或调用真实 Agent，不使用模型密钥。
./poc demo --executor opencode --output runs/demo-opencode
./poc demo --executor codex --output runs/demo-codex
.venv/bin/python -m unittest discover -s tests -v

# 需要真实执行器时，在该独立环境安装项目级依赖。
npm ci
./poc doctor
```

每次使用新的输出目录。Python 依赖固定到 Harness² commit `6868817518876de8fd170c6a8354daca273b6191`；CLI 固定为 `@opencode/cli@2.2.5` 和 `@openai/codex@0.144.6`。Codex 版本与该 Harness² commit 的参考版本一致。依赖保留各自的许可证。

## Codex 接入

通过 `--executor codex` 选择执行器，`--model` 直接传 Codex 支持的模型 ID。`run`、`worker`、`optimize` 均接受相同选项。CLI 默认从项目 `node_modules/.bin/codex` 查找，也可用 `--codex /ABS/PATH/codex` 显式指定。

在独立环境通过环境变量提供 `CODEX_API_KEY`。本项目只使用显式提供的这个自动化凭据，不回退到 `OPENAI_API_KEY`，也不复制个人 `~/.codex/auth.json` 或读取个人登录数据库。示例里的模型 ID 需替换为你的账户可用模型；密钥不要写入仓库。

```bash
# 在环境中设置 CODEX_API_KEY 后执行；不要把密钥写进这份命令示例。
./poc run --executor codex \
  --task examples/signed-total/task.json \
  --harness examples/base-harness \
  --output runs/codex-base \
  --model YOUR_CODEX_MODEL

./poc check \
  --run runs/codex-base/executions/task \
  --check-file examples/signed-total/check.py

./poc optimize --executor codex \
  --task examples/signed-total/task.json \
  --harness examples/base-harness \
  --output runs/codex-optimized \
  --model YOUR_CODEX_MODEL
```

`optimize` 中的 Solver 和 Editor 都由 Codex 执行，不需要 OpenCode 参与。仍使用 Google 原有递归算法；适配层保留共同的四文件搜索范围，并使用上游 Codex 轨迹渲染器。上游编辑器的 channel 枚举没有 Codex，因此本项目覆写编辑器 I/O；不会修改上游库，也不会悄悄切换到 OpenCode。

每次调用会创建独立的 `HOME`、`CODEX_HOME`、XDG 和临时目录，清除继承的 Codex 会话与端点配置，使用 `--ephemeral`、`--ignore-user-config`、`--ignore-rules`、`workspace-write` 和 `approval_policy=never`。模型认证网络与工具网络是两回事：工具的 sandbox 网络关闭，模型请求仍需访问 OpenAI。运行日志保存在指定输出目录；没有后台常驻服务或个人配置写入。

当前 Codex 路径仅接 OpenAI API Key，不支持 ChatGPT 登录复用、自定义模型供应商配置、app-server、resume/fork 或持续 Session。`--provider-config` 仅属于 OpenCode，传给 Codex 会明确报错。

## OpenCode 接入

保留默认执行器 `opencode`，旧命令继续可用。模型需为 `provider/model`；凭据通过该供应商的环境变量提供。

```bash
./poc run --executor opencode \
  --task examples/signed-total/task.json \
  --harness examples/base-harness \
  --output runs/opencode-base \
  --model YOUR_PROVIDER/YOUR_MODEL

./poc optimize --executor opencode \
  --task examples/signed-total/task.json \
  --harness examples/base-harness \
  --output runs/opencode-optimized \
  --model YOUR_PROVIDER/YOUR_MODEL
```

可用 `--opencode /ABS/PATH/opencode` 指定二进制。自定义端点通过 `--provider-config providers.json` 配置；仅允许顶层 `providers` 对象，格式遵循 OpenCode V2，凭据使用 provider 的 `env` 字段声明。每个执行使用独立的 OpenCode 数据目录，不复用个人登录数据库。

## 验收候选配置

优化只生成 `candidate_harness/`，不会替换当前配置。`k=1` 的选择会采用组合候选，不能据此宣称它优于 baseline。

1. 从 `optimization.json` 读取 `selected_run_dir`，执行 `poc check --run <目录> --check-file <独立检查脚本>`。
2. 使用相同执行器、模型、源代码与运行配置，把 `candidate_harness/` 传给普通 `run` 或真实 Worker 复测。
3. 业务收益实验需加入同预算多次尝试的对照，再在未见任务上验证。一次普通运行与 4 次 Solver＋Editor 的优化不能直接作公平比较。

OpenCode 与 Codex 之间切换时也需要重新验收；共享配置格式不意味着两者行为相同。独立检查脚本不会进入 Solver 的源代码快照，优化器不会收到检查分数。

## Super-agent Worker 接入

详细步骤见 [Worker 接入说明](integrations/dsh/README.md)。通过现有 `bash` 工具调用，无需修改 Worker 的工具白名单：

```bash
/ABS/POC/poc worker --executor codex \
  --codex /ABS/POC/node_modules/.bin/codex \
  --task /ABS/TICKET/task.json \
  --harness /ABS/HARNESSES/revision-001 \
  --output /ABS/RUNS/ticket-001/attempt-001 \
  --model YOUR_CODEX_MODEL
```

`worker` 额外检查 `DSH_SUPERAGENT_TICKET`、`DSH_SUPERAGENT_DIR` 与输入目录归属。长生命周期和后续输入仍由 DSH Worker 管理；一次委派新建一个 Agent 会话，结果中保留 Ticket ID。真实 DSH Host 的通知、取消和人工接管尚未验证。

## 输入与输出

任务 JSON 含 `id`、`domain`、`prompt`、`input_dir`、`deliverables`，路径相对任务文件。程序复制源代码快照，保留执行权限，拒绝符号链接、`.env` 与 OpenCode 项目配置。Codex 额外拒绝输入中的 `.codex/`、`.agents/`、`AGENTS.override.md` 和嵌套 `.env` 文件。输出目录必须位于输入目录之外，且不能复用已有目录。

```text
output/
  task.json                    输入指纹、执行器、模型、Ticket 与预算
  source/                      固定输入快照
  executions/<run-id>/
    workspace/                 代码及交付物
    harness/                   实际使用的四文件配置
    events.jsonl               所选执行器的原生事件，不转换成另一种协议
    stderr.log / prompt.txt / answer.txt
    result.json                状态、执行器、Session ID、用量、改动文件
    check.json                 执行独立验收后产生
    runtime/                   本次调用的独立用户目录与缓存
  editors/                     编辑器执行记录
  harness2/                    官方递归过程与选择记录
  candidate_harness/           待验证候选配置
  optimization.json            优化与用量摘要
```

退出码 0 还需匹配原生终态：OpenCode 为 `step_finish(reason=stop)`；Codex 需有 Thread、已开始且完成的 Turn，并且没有错误或未完成 Turn。失败工具调用可以由 Agent 恢复，不会仅因中间命令退出非零就把整个 Codex Turn 判失败。产物存在与 `completed` 均不代表业务验收通过。

Token/费用读取 `optimization.json` 的 `native_usage`。Codex 记录输入、缓存输入、输出、推理 Token，缓存输入是输入 Token 的子集；原生事件不提供价格，`cost_usd_reported` 为 `null`。缺失用量由 `all_runs_reported_usage=false` 标记。上游通用 ledger 的默认零费用不能作为账单。

## 原生 CLI 协议冒烟测试

仅在准备好的独立环境运行，测试服务使用回环地址，均不需要真实模型密钥：

```bash
.venv/bin/python tests/codex_smoke.py \
  --codex ./node_modules/.bin/codex --output runs/native-codex
.venv/bin/python tests/opencode_smoke.py \
  --opencode ./node_modules/.bin/opencode --output runs/native-opencode
```

这些测试验证真实 CLI 与模拟模型的接口，不验证模型推理或编码质量。本次 Codex 原生测试按要求留给 GitHub Runner，没有在作者本机安装或启动 Codex CLI。

## 边界

- 仅支持 `k=1` parallel；最多 4 次 Solver、6 次 Editor 调用，每个进程有超时。次数和时间预算不等于费用硬上限。
- `SIGINT`、`SIGTERM`、超时会清理子进程组；断电、`SIGKILL`、跨主机进程不在保证内。中断后检查旧目录，使用新目录重跑。
- 快照和独立用户目录不等于整机隔离。Codex 使用其原生 sandbox；OpenCode shell 仍以启动用户执行。真实业务实验应使用固定、干净的隔离环境；机器级管理策略仍可能影响运行。
- 代码产物留在快照里，不自动写回 Ticket、不发消息、不创建 PR、不发布配置。Worker 审核并应用需要的改动后，再走原有交付流程。
- 真实 DSH Host、真实模型、同预算质量对照仍未验证。

接口依据：[Harness² 固定版本](https://github.com/google-research/harness2/tree/6868817518876de8fd170c6a8354daca273b6191)、[Codex 非交互模式](https://developers.openai.com/codex/noninteractive)、[Codex 配置](https://developers.openai.com/codex/config-reference)、[Codex 固定版本事件协议](https://github.com/openai/codex/blob/rust-v0.144.6/sdk/typescript/src/events.ts)、[OpenCode CLI](https://opencode.ai/v2/docs/cli/commands/)。
