# 在 Super-agent Worker 中调用

官方 Super-agent Worker 的现有 `bash` 工具可以运行本 PoC。采用 shell 入口后，不需要把 `subagent_opencode` 或 `harness2_run` 加入它的工具白名单。此目录提供调用约定；尚未改动或部署任何 DSH profile。

1. 在运行 DSH 的隔离环境安装本项目与所选执行器，确保 Worker 能访问同一绝对路径。使用 Codex 时提供 `CODEX_API_KEY`；不复制个人登录数据库。
2. 给 Ticket 准备任务 JSON，`input_dir` 指向该 Ticket 工作目录内的源代码快照。验收脚本由操作者另行保存。
3. 给本 Ticket 固定一份 Harness 目录，后续运行继续传同一版本。输出目录放在 Ticket 输入快照之外，按任务尝试分开。
4. Worker 调用以下命令。路径由部署者配置为真实绝对路径，不直接拼接外部消息中的 shell 文本。

```bash
/ABS/POC/poc worker --executor opencode \
  --task /ABS/TICKET/task.json \
  --harness /ABS/HARNESSES/revision-001 \
  --output /ABS/RUNS/ticket-001/attempt-001 \
  --opencode /ABS/POC/node_modules/.bin/opencode \
  --model YOUR_PROVIDER/YOUR_MODEL
```

改用 Codex 时，四文件 Harness、任务格式与输出格式保持一致：

```bash
/ABS/POC/poc worker --executor codex \
  --task /ABS/TICKET/task.json \
  --harness /ABS/HARNESSES/revision-001 \
  --output /ABS/RUNS/ticket-001/codex-attempt-001 \
  --codex /ABS/POC/node_modules/.bin/codex \
  --model YOUR_CODEX_MODEL
```

对应的优化命令同样加 `--executor codex`。Solver 和 Editor 均使用 Codex，不需要 OpenCode 参与；复测必须固定同一执行器、模型和 Harness 版本。模型参数是 Codex 模型 ID，不加 OpenCode 的 provider 前缀。

`DSH_SUPERAGENT_TICKET` 与 `DSH_SUPERAGENT_DIR` 由官方 Worker 执行环境提供。入口要求它们存在，并拒绝读取其他 Ticket 的输入目录。环境变量只是宿主提供的调用上下文，不是额外的身份认证边界。

Super-agent 会把长 shell 命令转为后台任务，父 Worker 应结束当前回合并等待原有 job 通知；收到结果后检查 `status`、`result.json`、代码改动和独立验收结果，再使用已有的 `orchestrator_report`。不要把 JSON 的 `completed` 直接当作业务 `ready`。

本版采用单次 OpenCode / Codex 执行，长生命周期与后续输入仍属于 DSH Worker。一次新的委派产生新 Session，不代表恢复旧会话。父 Worker 应把必要的历史事实放入新任务输入。Codex 使用独立 HOME/CODEX_HOME、临时 Thread 和原生 workspace-write sandbox；执行结果、取消与 Token 的统一处理保持一致，费用未提供时为 null。

代码产物位于结果中的 `workspace`。Worker 应审核 `changed_files`，把需要的改动应用回 Ticket 工作目录并重跑测试，然后沿用原有 PR 流程。PoC 本身不向 GitHub、飞书发送消息或发布改动。

端到端验收要在真实 Host 上确认：Ticket 环境可见、后台任务结果可达、取消后无子进程残留、父 Worker 的报告和人工接管正常。当前自动化测试覆盖的是该 CLI 调用约定，不代表这些 Host 生命周期行为已验证。
