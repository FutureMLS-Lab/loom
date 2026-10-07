# Loom CI Development Onboarding

这是一份可以直接发给工程师或 Agent 的端到端操作手册。目标是从一台
可信控制机（默认 `b2`）创建 Loom Development Task，让正确的 Cursor
implementer 在隔离 worktree 中开发，由两个独立 reviewer 审阅同一个提交，
并在需要时通过受限 kubeconfig 使用 CI Kubernetes 集群。长时间 CPU/GPU
步骤由 durable Experiment Run 驱动，不依赖 Agent 会话一直在线。

```text
laptop/browser
    │ SSH tunnel
    ▼
b2: Loom + Cursor Agent + git worktrees + credentials
    │
    ├── Development Task implementer（唯一 writer）
    ├── Reviewer A（correctness / testing，read-only）
    ├── Reviewer B（architecture / operability，read-only）
    └── durable experiment controller
              │ restricted kubeconfig
              ▼
       CI Kubernetes worker / Job
       无 Agent 凭据 · digest-pinned image · PVC artifacts
```

## 0. 开始前的 capability gate

不要仅凭 Loom 页面能打开就开始任务。部署版本必须同时包含 Development
Task 和 Experiment Run v1：

```bash
cd "$HOME/loom"
test -f loom/development_task.py
test -f loom/skills/dev/sde-implementer/SKILL.md
test -f loom/skills/dev/sde-correctness-review/SKILL.md
test -f loom/skills/dev/sde-architecture-review/SKILL.md
loom experiment --help
grep -q 'Development — implement' loom/web_static/index.html
```

任何一条失败都先停止。不要临时复制几个 Python 文件拼一个部署，也不要在
共享 b2 上直接切到未经集成验证的分支。

截至 2026-10-06，Development Task 的已验证实现位于
`codex/token-budget-optimization`（checkpoint `5c338f9`），Experiment Run
v1 位于 Loom 的持久实验改动中。正式 onboarding 应使用同时包含两者、通过
测试并由团队批准的集成 commit。这个说明只是发布门槛，不是让操作者在生产
主机上手工 merge 两条分支。

## 1. 一次性准备 b2

如果 b2 已经有健康的 Loom，跳到第 2 节。否则在 b2 上执行：

```bash
python3 --version                   # >= 3.10
git --version
tmux -V
kubectl version --client
agent --version
agent status --format json
loom doctor
```

安装和首次登录的完整步骤在 `AGENT.md` 和
`loom/skills/server_setup/server_setup.md`。Agent 登录必须由用户本人完成：

```bash
NO_OPEN_BROWSER=1 agent login
agent status --format json
```

不要复制其他人或其他组织的 `~/.cursor`、SSH key、GitHub token 或
kubeconfig。b2 是控制面，不是 GPU worker；Agent、源码编辑、Git 凭据和
集群凭据都留在这里。

### 验证模型，而不是假设模型存在

Cursor 模型目录会变化，创建任务前检查当前账号真实暴露的 ID：

```bash
agent --list-models | grep -E 'claude-opus-5-5-max-fast|grok-4.7-high-fast'
```

推荐选择：

| 工作类型 | 模型 |
|---|---|
| 普通迭代、明确 bug、成本敏感 | `grok-4.7-high-fast` |
| 跨仓库、复杂协议、研究型系统、CI/GPU 调试 | `claude-opus-5-5-max-fast`，仅当上面的命令真实列出它 |

不要把一个不存在的自定义 model ID 填进 Loom。Development Task 当前使用
同一个所选模型运行 implementer、Reviewer A 和 Reviewer B。

### 启动或验证 Loom

推荐 b2 使用端口 `8766`，避免和本机的 `8765` 冲突。已有服务时不要再起
第二份：

```bash
tmux ls | grep loom
curl -sS -o /dev/null -w '%{http_code}\n' \
  -u "x:$LOOM_TOKEN" http://127.0.0.1:8766/
```

需要新启动时：

```bash
cd "$HOME/loom"
tmux new-session -d -s loom-b2 \
  "LOOM_WEB_AUTH_TOKEN=$LOOM_TOKEN loom web --projects --project $HOME/work --port 8766"
```

`LOOM_TOKEN` 从密码管理器或受限 env file 读取；不要写进 Git、PLAN.md、
终端截图或任务 prompt。Loom 必须继续绑定 `127.0.0.1`。

从 laptop 连接：

```bash
ssh -N -L 8766:127.0.0.1:8766 b2
```

然后打开 `http://127.0.0.1:8766`，任意用户名，`LOOM_TOKEN` 作为密码。

## 2. 一次性准备 CI cluster 访问

凭据只放在 b2 的受限目录，例如：

```bash
install -d -m 0700 "$HOME/agent-resources"
chmod 0600 "$HOME/agent-resources/ci-cluster.kubeconfig"
export KUBECONFIG="$HOME/agent-resources/ci-cluster.kubeconfig"
```

不要把 kubeconfig 内容粘贴给 Agent。给 Agent 的是文件路径，以及已经批准的
context、namespace、PVC、GPU label、普通 priority class 和并发额度。

先建立本次任务的非秘密变量：

```bash
export LOOM_CI_CONTEXT='<approved-context>'
export LOOM_CI_NAMESPACE='<approved-namespace>'
export LOOM_CI_PVC='<approved-pvc>'
export LOOM_GPU_LABEL='nvidia.com/gpu.product=<approved-product>'
export LOOM_WORKER_IMAGE='registry/repository@sha256:<64-lowercase-hex>'
```

只读检查：

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  get namespace "$LOOM_CI_NAMESPACE"
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" auth can-i get pods
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" get pvc "$LOOM_CI_PVC"
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  get nodes -l "$LOOM_GPU_LABEL"
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" get resourcequota
```

`Forbidden`、找不到 PVC、没有符合 label 的 node，或镜像没有 immutable
digest 时都停止。不要通过换 namespace、借用他人的 ServiceAccount、提升
priority 或复制 broad kubeconfig 来绕过。

再运行 Loom 的只读 preflight：

```bash
python "$HOME/loom/loom/skills/run-remote-gpu-worker/scripts/gpu_worker_preflight.py" \
  --kubeconfig "$KUBECONFIG" \
  --namespace "$LOOM_CI_NAMESPACE" \
  --gpu-label "$LOOM_GPU_LABEL" \
  --pvc "$LOOM_CI_PVC" \
  --image "$LOOM_WORKER_IMAGE"
```

这一步只说明基质是否可用，不会创建 Pod。看不到其他 namespace 的 GPU
allocation 时，结果是 `unknown`，不是“当前无人使用”。

## 3. 注册代码仓库

仓库必须已经存在于 b2，而且目标 base 必须明确：

```bash
git -C /absolute/path/to/repo fetch origin
git -C /absolute/path/to/repo status --short
git -C /absolute/path/to/repo branch --show-current
git -C /absolute/path/to/repo rev-parse origin/main
```

不要在一个有用户未提交修改的 checkout 上做自动清理或 reset。在 Loom 中：

1. 点击 **Add folder**。
2. 注册 `/absolute/path/to/repo`；如果 Loom 以多项目模式启动，也可以在
   `$HOME/work` 下 clone。
3. 确认 project 的 code root 指向真正的 git root，而不是父目录。

Development Task 创建后会自动生成隔离 worktree 和 `loom/<task-slug>`
分支。implementer 只允许修改这个 worktree。

## 4. 创建正确的 Development Task

点击 **Create Task**，填写：

| 字段 | 推荐值 |
|---|---|
| Task type | **Development — implement, then two independent code reviews** |
| Title | 短、可搜索，例如 `glm53-responses-conformance` |
| Model | 第 1 节实际验证过的模型 |
| Test command | 最能代表 merge gate 的确定性命令，例如 `cargo test --workspace` |
| Maximum review rounds | `2` |
| Skills | 需要 CI/GPU 时选择 `run-remote-gpu-worker`；纯本地任务不选 |
| General goal | 使用下面的任务合同模板 |

不要手工选择 `sde-implementer`、`sde-correctness-review` 或
`sde-architecture-review`：Development Task 会按角色自动注入它们。

### General goal 模板

```text
Outcome
- 在 <repo> 最新 origin/main 的隔离 worktree 上实现 <具体功能/修复>。
- 不修改 production、live config、共享模型权重或无关资源。

Acceptance
- 用 <test command> 复现当前失败并保存 baseline。
- 实现后通过同一套 local/fake E2E。
- 若涉及推理或 GPU，必须通过真实模型经真实 frontend 的 CI E2E；fake-only green 不算完成。
- 所有结论附命令、退出码、case counts 和 artifact 路径。
- 形成 clean、DCO Signed-off-by commit；不 push、不 merge。

Execution
- 先读仓库 AGENTS.md/CLAUDE.md 和最小相关代码。
- 使用 $run-remote-gpu-worker；控制面留在 b2。
- outcome-bearing 长任务先建立 durable `loom experiment` run。
- CI worker 使用批准的 kubeconfig/context/namespace/PVC、digest-pinned image、
  普通 priority、准确 GPU 数量和最长 5 小时 lifetime。
- CPU prepare 与 GPU execution 分开；正式 runtime 不安装依赖。

Review
- Reviewer A 检查 behavior、failure safety、data integrity 和回归证据。
- Reviewer B 检查 boundaries、integration、lifecycle、rollback、cost 和维护性。
- 最多两轮；修复所有 P0/P1。P2/P3 留给 human gate 决定。

Deliverables
- commit SHA 和 Signed-off-by
- local/fake test summary
- real CI/model test summary
- image/source/env/input digests
- logs/results/artifact manifest
- 已创建和已清理的 Kubernetes 资源准确名称
- remaining risks / true external blockers
```

创建后检查 Development 面板是否显示：

- 一个隔离 worktree；
- Reviewer A：Behavioral correctness & verification；
- Reviewer B：Architecture, integration & operability；
- 正确 reviewer model；
- `0/2 rounds`。

## 5. 启动 implementer

点击 **Start Agent**。正常情况下使用 Cursor Agent；Development Task 不会
因为 UI 里叫“Agent Terminal”就变成多个 writer。

首次消息可以直接使用：

```text
Read PLAN.md and the repository instructions. You are the sole writer in this
Development Task. Establish the exact baseline first, then implement and verify
the smallest cohesive fix. Use $run-remote-gpu-worker for authorized CI/GPU
work. Keep Loom, source editing, credentials, and reasoning on b2. Before any
outcome-bearing long run, create and detach a durable `loom experiment` run.
Do not stop at fake-gRPC or mocked E2E if the acceptance criteria require the
real model path. End with a clean DCO commit, exact test counts, artifact paths,
and remaining risks. Do not push or merge.
```

Agent 应当先完成下面的 CPU 工作，再申请 GPU：

1. 记录 base SHA、依赖版本和 baseline failures。
2. 聚类失败，优先修协议层根因，不逐 case 打补丁。
3. 本地编译和窄测试。
4. fake engine E2E。
5. 生成 immutable execution bundle 和 CI preflight。
6. canary 成功后才运行完整真实模型 E2E。

## 6. 把长程 CI/GPU 工作放进 Experiment Run

Agent 会为本次实验准备小型 wrapper scripts。脚本输出必须写入
`$LOOM_EXPERIMENT_RUN_DIR` 下的 `artifacts/`、`logs/` 或 `forensics/`，
不能只留在 Pod 临时文件系统。

Experiment Run v1 当前从 run directory 启动命令，因此 spec 中应使用
wrapper 的绝对路径，或者先把 wrapper 放进 run directory；不要假设命令
相对 Development worktree 执行。

最小 spec 结构：

```json
{
  "run_id": "glm53-conformance-001",
  "source_revision": "<40-hex-clean-commit>",
  "image": "registry/repository@sha256:<64-lowercase-hex>",
  "environment_digest": "sha256:<64-lowercase-hex>",
  "inputs": {
    "test-manifest": "sha256:<64-lowercase-hex>"
  },
  "stages": [
    {
      "name": "CPU_PREP",
      "kind": "PREPARE",
      "command": ["bash", "/absolute/task/worktree/ci/prepare.sh"],
      "hermetic": false,
      "artifacts": ["artifacts/prepared-environment.json"]
    },
    {
      "name": "SUBSTRATE_CHECK",
      "kind": "VALIDATE",
      "command": ["bash", "/absolute/task/worktree/ci/validate.sh"],
      "artifacts": ["artifacts/substrate-validation.json"]
    },
    {
      "name": "CANARY",
      "kind": "CANARY",
      "command": ["bash", "/absolute/task/worktree/ci/canary.sh"],
      "artifacts": ["artifacts/canary.json"]
    },
    {
      "name": "FORMAL_E2E",
      "kind": "RUN",
      "command": ["bash", "/absolute/task/worktree/ci/formal-e2e.sh"],
      "artifacts": ["artifacts/formal-e2e.json"]
    },
    {
      "name": "COLLECT",
      "kind": "COLLECT",
      "command": ["bash", "/absolute/task/worktree/ci/collect.sh"],
      "artifacts": ["artifacts/report.md"]
    }
  ],
  "cleanup": {
    "command": ["bash", "/absolute/task/worktree/ci/cleanup.sh"]
  }
}
```

启动：

```bash
cd /absolute/path/to/project
loom experiment create /absolute/path/to/experiment.json
loom experiment drive \
  ".RUD/experiments/glm53-conformance-001" --detach
loom experiment status \
  ".RUD/experiments/glm53-conformance-001"
```

关键语义：

- `spec.json` 冻结后不允许修改；改变 source/image/env/input 就创建新 run。
- `SUCCESS.json` 或 `FAILURE.json` 在 cleanup 之前原子写入。
- cleanup 失败不会把成功实验改成失败。
- outcome-bearing worker 消失时变成 `BLOCKED`，不会静默重跑。
- 检查证据后才能显式执行：

  ```bash
  loom experiment retry .RUD/experiments/glm53-conformance-001 \
    --stage FORMAL_E2E
  ```

- hermetic stages 中出现 runtime `pip/npm/apt/uv/go install` 会 fail-fast。
  缺失依赖应烘焙进 image/wheelhouse，或在 CPU_PREP 阶段完成并验证 digest。

## 7. 创建短寿命 CI worker

先渲染，不直接 apply：

```bash
python "$HOME/loom/loom/skills/run-remote-gpu-worker/scripts/render_worker_bundle.py" \
  --name '<task-scoped-worker-name>' \
  --namespace "$LOOM_CI_NAMESPACE" \
  --image "$LOOM_WORKER_IMAGE" \
  --pvc "$LOOM_CI_PVC" \
  --mount-path '<approved-mount-path>' \
  --work-root '<approved-mount-path>/<task-scoped-root>' \
  --gpu-label "$LOOM_GPU_LABEL" \
  --hours 5 \
  --output /tmp/loom-worker.yaml
```

逐行检查 Pod、ServiceAccount、Role 和 RoleBinding，再执行：

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" diff -f /tmp/loom-worker.yaml
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" apply -f /tmp/loom-worker.yaml
```

worker 只得到它需要的 GPU 和 PVC。Agent 凭据、GitHub token、SSH key 和
broad kubeconfig 不进入 Pod/PVC。controller access 只允许指定 Pod 的
`get/watch/log/exec`；资源创建、删除和 token 撤销留在可信 provisioning
侧。详细 RBAC 命令在
`loom/skills/run-remote-gpu-worker/references/kubernetes-patterns.txt`。

每次正式运行必须记录：

- source commit 和 dirty status；
- requested image digest 与实际 runtime image ID；
- environment/wheelhouse digest；
- test manifest/input hashes；
- node、GPU type、start/end 和 GPU-hours；
- exact command、exit code、passed/failed/skipped counts；
- Job/Pod YAML、events、stdout/stderr、termination reason；
- results 和 cleanup 状态。

CPU preparation 与 GPU execution 应拆开。不要让 GPU Pod 等待下载、pip
install、编译无关依赖或 Agent 思考。canary 通过之后才展开矩阵。

## 8. 提交 checkpoint 并运行双 Reviewer

implementer 完成后必须留下 clean、DCO-signed checkpoint：

```bash
git status --short                       # 必须为空
git diff --check
git commit -s -m '<cohesive message>'
git log -1 --format='%H%n%B'
```

在 Development 面板点击 **Run reviewers**。Loom 会：

1. 拒绝 dirty worktree、无新 commit 或重复审阅同一个 commit；
2. 为 exact candidate SHA 创建 detached immutable snapshot；
3. 并行启动两个 fresh read-only sessions；
4. Reviewer A 只负责 correctness/testing/failure/data safety；
5. Reviewer B 只负责 architecture/integration/lifecycle/cost/maintainability；
6. 某 reviewer 失败时只重跑缺失 reviewer；
7. P0/P1 进入 `repair_needed`；无阻断 finding 或达到轮数上限时进入
   human gate。

有 P0/P1 时点击 **Start repair**。Loom 会启动新的 implementer session，
但继续使用同一个 writer worktree。修复、补回归测试、再次 `git commit -s`，
然后运行第二轮 reviewers。

默认最多两轮。最后在 human gate 检查 exact HEAD、测试证据和所有 P2/P3，
确认后点击 **Approve for merge**。这个按钮只记录批准；Loom 不会 push 或
merge。push/PR/merge 仍由用户明确执行。

## 9. 监控和恢复

不要把“tmux 里还有进程”当成任务正在进展。至少同时检查：

```bash
loom experiment status /absolute/project/.RUD/experiments/<run-id>
stat /absolute/project/.RUD/experiments/<run-id>/run.json
find /absolute/project/.RUD/experiments/<run-id>/attempts -name result.json -print
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" get pod,job
```

| 现象 | 正确动作 |
|---|---|
| Agent session stalled | reconnect/rotate Agent；detached Experiment Run 不需要重启 |
| Loom Web 重启 | 同一 project 启动 Loom 后自动恢复未完成 experiment controller |
| Stage `BLOCKED` | 保存现场、读 attempt logs/result；确认安全后显式 retry |
| runtime install 被拒绝 | 修 image/wheelhouse/CPU_PREP，不关闭 hermetic guard |
| Pod 被 reaper 删除 | 用已落盘 events/logs/YAML 判断；不要凭记忆重跑 |
| Reviewer error | 再点 Run reviewers；只补跑缺失 reviewer |
| Review 按钮拒绝 | 先 clean commit；candidate 必须不同于 base/last reviewed |
| model ID 不可用 | `agent --list-models` 重查并选择真实 ID |
| cluster `Forbidden` | 停止并找 owner 修 RBAC；不自行升级权限 |

## 10. 完成和清理

先确认结果和哈希已经回到 b2/PVC 的 durable task root，再删除**本任务创建的
精确资源**：

```bash
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" get pod,job
kubectl --kubeconfig "$KUBECONFIG" --context "$LOOM_CI_CONTEXT" \
  -n "$LOOM_CI_NAMESPACE" delete -f /tmp/loom-worker.yaml
```

删除前重新打开 YAML 核对 name/namespace。不要使用宽泛 label selector，
不要清理其他 task、其他用户、共享 PVC、共享模型权重、production deployment
或 live config。撤销 task-scoped controller token/RBAC；cleanup 自己失败时保留
成功 verdict，同时把 cleanup failure 明确报告出来。

最终 handoff 至少包含：

```text
Task / branch:
Base SHA:
Candidate SHA + Signed-off-by:
Files changed:
Local/fake tests (command + counts):
Real CI/model tests (command + counts):
Experiment run id/status:
Image/source/environment/input digests:
Artifact paths:
GPU-hours:
Resources created:
Resources removed / intentionally retained:
Reviewer A verdict/findings:
Reviewer B verdict/findings:
Known risks / blockers:
Human decision needed:
```

## 11. 十分钟 onboarding smoke test

在让新人承担客户任务前，用一个 disposable repository 验证整条链路：

1. 创建 Development Task，test command 是一个小型 deterministic test。
2. implementer 增加一个函数和回归测试，提交 clean `git commit -s`。
3. 两位 reviewer 都返回结构化 verdict。
4. 若此人获准使用 CI，运行 read-only preflight；不要为了 onboarding 占用 GPU。
5. 创建一个纯 CPU 的两阶段 Experiment Run，验证 detached controller、
   `SUCCESS.json`、artifact hashes 和独立 cleanup。
6. 重启 Loom Web，确认 task、review state 和 experiment state 仍能恢复。
7. 在 human gate 审阅 exact SHA；不要真的 push/merge disposable change。

只有这七步全部通过，才把客户或生产相关任务交给这套环境。

## 12. 不可违反的边界

- 不把 token、private key、kubeconfig 内容或 Agent login state写进 Git、
  PLAN.md、prompt、日志和 PVC。
- 不在 GPU worker 里运行 Loom/Agent，也不把 broad cluster credential 放进去。
- 不修改 production、serverless live config 或运行中的客户 deployment，除非任务
  明确授权且有独立 rollout plan。
- 不 reset、clean、覆盖用户已有 dirty worktree。
- 不删除共享模型权重、PVC 或不属于本任务的 Pod/Job。
- 不以 fake-only、mock-only 或“进程还活着”宣称真实 E2E 完成。
- 不静默重跑 outcome-bearing attempt；每个 retry 都有新 attempt identity。
- 不让 cleanup 改写已经原子落盘的实验 verdict。
- 不让 reviewer 编辑 candidate；implementer 永远是唯一 writer。
- Loom 不自动 push/merge；外部状态改变需要用户明确授权。

## 深入参考

这篇文档足以走完 golden path。需要排障或修改基础设施时再查：

- `AGENT.md`：最小 Loom 部署。
- `loom/skills/server_setup/server_setup.md`：完整 b2/Agent host setup。
- `loom/skills/run-remote-gpu-worker/SKILL.md`：remote worker 安全合同。
- `loom/skills/run-remote-gpu-worker/references/kubernetes-patterns.txt`：RBAC、
  sync、launch 和 rotation 命令。
- `docs/EXPERIMENT-RUNS.md`：Experiment Run v1 状态与 artifact 合同。
- `loom/skills/remote_control/remote_control.md`：外部 controller/API 操作。
- `docs/API.md`：Development Task 与普通 Task HTTP API。
