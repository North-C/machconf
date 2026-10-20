# machconf — 机器配置检测工具

通过 SSH 免密登录批量检测服务器的 BIOS / OS / NUMA / 硬件配置, 并核对机器上部署的
CubeSandbox 组件是否与社区 v0.7.1 原版一致, 输出对齐表格、JSON 或 Markdown 报告。面向性能测试前的机器基线核对 (NUMA 拓扑、大页、内存频率、
数据盘类型等), x86 与 ARM 机型均可使用, 平台不可用项优雅降级为 `N.A.` 并说明原因。

## 前置条件

- Python ≥ 3.8, 仅标准库
- 远程模式: 目标机 `ssh root@<host>` 免密可登录; 或加 `--ask-pass` 交互输入密码
- 本机模式 (`--local`): 无需 SSH; BIOS/内存等完整信息仍需 root (sudo) 运行

## 快速开始

```bash
# 本机检测, 无需 SSH
sudo python3 machconf.py --local

# 远程全模块检测, 表格输出
python3 machconf.py -H 192.0.2.10

# 无免密时交互输密码 (终端前台运行; 同主机 1 分钟内复用连接, 只输一次)
python3 machconf.py -H 192.0.2.10 --ask-pass
python3 machconf.py -H 192.0.2.10,192.0.2.11 --ask-pass

# 单模块 (bios / os / numa / hardware / cube / cube-runtime / kvm-arm / rcu)
python3 machconf.py --local -m numa
python3 machconf.py -H 192.0.2.10 -m numa

# CubeSandbox 组件对比社区 v0.7.1 (需 root; 非默认安装目录用 --cube-root)
python3 machconf.py -H 192.0.2.10 -m cube
python3 machconf.py -H 192.0.2.10,192.0.2.11 -m cube -f csv-wide > cube-compare.csv
sudo python3 machconf.py --local -m cube --cube-root /opt/cubetoolbox

# 多台汇总
python3 machconf.py -H 192.0.2.10,192.0.2.11

# 两台机器并排对比，支持只看差异
python3 machconf.py -H 192.0.2.10,192.0.2.11 --compare
python3 machconf.py -H 192.0.2.10,192.0.2.11 --compare --diff-only -m numa,rcu,cube-runtime

# 输出格式: table (默认) / json / markdown / csv / csv-wide
python3 machconf.py -H 192.0.2.10 -f markdown > report.md
python3 machconf.py -H 192.0.2.10 -f csv > report.csv           # CSV 长表 (Excel/pandas)
python3 machconf.py -H 192.0.2.10,192.0.2.11 -f csv-wide > compare.csv  # 宽表多机对比

# 修改 numa_balancing (立即生效, 回读验证; 重启后恢复)
sudo python3 machconf.py --local --set-numa 0
python3 machconf.py -H 192.0.2.10 --set-numa 0
```

## 两台机器配置对比

支持在线采集后对比，或读取两份已有的 JSON 报告离线对比。两种方式均为只读，
输出列为 **模块 / 配置项 / 左机器值 / 右机器值 / 结果**。
默认采集全部模块并保留相同项，展示顺序为 **硬件 → BIOS → OS → NUMA → RCU →
KVM ARM → Cube 组件 → Cube 运行资源**；离线报告中的其他模块追加到末尾。

```bash
# 在线：必须指定两台不同机器，左右顺序与 -H 一致
python3 machconf.py --compare -H 192.0.2.10,192.0.2.11 -f markdown > comparison-full.md
python3 machconf.py --compare -H 192.0.2.10,192.0.2.11 -m numa,rcu,cube-runtime
python3 machconf.py --compare -H 192.0.2.10,192.0.2.11 -m bios,os,numa,hardware \
  --diff-only -f markdown > comparison.md

# 分别采集并保存；每份文件仅包含一台机器
python3 machconf.py -H 192.0.2.10 -f json > machine-a.json
python3 machconf.py -H 192.0.2.11 -f json > machine-b.json

# 离线：不连接 SSH，可以比较本机与远程机保存的报告
python3 machconf.py --compare-files machine-a.json machine-b.json
python3 machconf.py --compare-files machine-a.json machine-b.json --diff-only -f markdown > comparison.md
python3 machconf.py --compare-files machine-a.json machine-b.json -f csv > comparison.csv
python3 machconf.py --compare-files machine-a.json machine-b.json -m rcu,numa -f json > comparison.json
```

`--compare-files` 接受主工具 `machconf.py -f json` 的输出，不接受 `cube_runtime.py` 的独立 JSON
结构或已生成的 comparison JSON。若只关注 Cube 资源，保存时用 `machconf.py -m cube-runtime -f json`。
本机报告用 `machconf.py --local -f json` 获取。文件中的主机名可相同，用于比较同机不同时间的快照；
表格明确标识左右两侧。

| 结果 | 含义 |
| --- | --- |
| 相同 | 两侧采集值相同且可判定 |
| 不同 | 两侧均有可判定的值，但值不同 |
| 仅左侧 / 仅右侧 | 另一份报告没有该项；不等于该机器没有此配置 |
| 不可判定 | 至少一侧为不可用、权限不足、未观测到、脱敏值或不完整读取结果等 |
| 多项歧义 | 同侧同一模块内出现重复配置项，保留全部证据，不猜测配对或覆盖 |

对比按 **模块名 + 配置项名称** 对齐，不混淆不同模块的同名项。Cube 资源进程项的 `pid=数字`
在匹配时归一为 `pid=*`，原始条目仍保留；同组件多个进程造成重名时显示“多项歧义”。
Cube 资源采集时间和正常进程扫描状态不参与配置对比；异常扫描仍保留。

比较以 `value` 为准，`note` 是来源/诊断上下文，不参与判等，完整保留在对比 JSON 中。
字典形式的 JSON 值不受字段顺序影响；普通字符串、单位、数组顺序、线程数量和其他路径保持原样，
不自动推断单位换算、默认值或组件实例映射。因此“相同”是采集值相同，不代表完整机器状态一致。
`N.A.` 等不可用值即使两边相同也不会判为“相同”。

`--diff-only` 只隐藏“相同”，不会隐藏缺项、不可判定或歧义。
终端表格与 Markdown 显示完整状态汇总；JSON 保留全量汇总及展示条目数；
CSV 是带 UTF-8 BOM 的矩形对比表（对比模式下 `csv-wide` 与 `csv` 相同）。
长表格值完整折行，不截断；普通多机报告和原有 `csv-wide` 用法保持兼容。
Markdown 单元格使用行内代码保留配置原文，方括号、下划线等不转换成 HTML 实体；
仅表格分隔符 `|` 使用反斜杠转义，多行值使用 `<br>` 换行。

在线模式任一主机采集失败则退出 1，不生成缺少一侧的成功对比。
报告格式错误、所选模块没有可对比项等也退出 1；成功生成对比退出 0，存在差异本身不算执行失败。
对比选项不能与 NUMA/THP/HugeTLB/KVM 修改选项混用。

## 模块

| 模块 | 内容 | 主要数据源 |
| --- | --- | --- |
| `bios` | BIOS 固件/整机型号、CPU 核心×路×线程 (SMT)、NUMA 节点及各节点 CPU 分布、governor/C-state/EPB (功耗)、睿频/超频 | dmidecode, lscpu, cpufreq sysfs |
| `os` | 发行版/内核版本、静态大页 (容量/空闲/各 NUMA 节点分布)、透明大页 THP | /proc/meminfo, /sys/devices/system/node |
| `numa` | `kernel.numa_balancing` 运行时开关、内核启动参数覆盖、sysctl 持久化配置 | /proc/sys, /proc/cmdline, sysctl.conf |
| `hardware` | 内存容量/类型/频率 (实配与标称)、磁盘大小/类型 (NVMe/SSD/HDD)/角色 (系统盘/数据盘/未挂载) | dmidecode, lsblk |
| `cube` | CubeSandbox 各组件文件、运行进程、容器镜像与社区 v0.7.1 离线包是否一致 | sha256sum, /proc/\<pid\>/exe, docker |
| `cube-runtime` | CubeMaster / Cubelet / CubeAPI 的 Go/worker 参数、CPU/NUMA、内存、文件句柄和并发资源配置 | systemctl, /proc, cgroup 文件系统 |
| `rcu` | RCU expedited / normal 运行值、normal_after_boot、cmdline 及内核配置证据 | sysfs, /proc/cmdline, /proc/config.gz 或 /boot/config-* |
| `kvm-arm` | ARM KVM 的 GICv4 / virt MSI bypass / DVMBM / IPIv 启动参数、默认引导项及内核日志 | /proc/cmdline, /etc/default/grub, grubby, dmesg |

输出格式: `table` 终端对齐表格; `json` 结构化; `markdown` 报告; `csv` 长表
(主机/模块/配置项/值/说明, 一行一项, 带 UTF-8 BOM 可直接用 Excel 打开);
`csv-wide` 宽表 (一行一个配置项、每台主机一列, 适合多机配置差异对比)。

说明:

- BIOS 菜单项无法在运行中的 OS 内直接读取 (需厂商带外工具), `bios` 模块检测的是
  BIOS 设置在 OS 侧的可见结果; 不同机型/架构可用项不同。
- 超频检测: x86 走 `intel_pstate/no_turbo`; ARM 等平台按最大频率与基础频率对比
  推断 (标注"推断")。
- 磁盘角色按挂载点判别, 逐盘 `lsblk` 聚合含 LVM 子设备的挂载点, 系统盘 = 挂载了
  `/`、`/boot` 或 swap 的盘, 其余已挂载盘为数据盘。

## CubeSandbox 组件对比 (cube 模块)

判断机器上部署的 CubeSandbox 是否就是社区发布的 v0.7.1 (只读, 不做任何修改)。
基准清单在 `refs/` 下, 原样取自 cube-perf-bench (`cpb.py release-ref` 由社区
one-click 离线包生成的 `release/v0.7.1-<arch>.json`, 及 `pins.json` 中的容器镜像 ID),
判定口径与 `cpb.py verify-deploy` 一致; 按目标机 `uname -m` 选 aarch64 / x86_64 基准。

| 对比对象 | 方法 | 是否计入结论 |
| --- | --- | --- |
| 二进制 (ELF / 内核 / guest 镜像 / agent 盘) | 安装目录下逐个文件 SHA256 | 是, 不同或缺失即"不一致" |
| 运行进程 | `/proc/<pid>/exe` 的 SHA256 (可发现启动后被替换的二进制) | 是 |
| 容器 cube-egress / cube-lifecycle-manager / cube-proxy | docker image ID (config digest, 与仓库/标签无关) | 运行中容器镜像不同计入 |
| 脚本 / 配置 / 软链接 | SHA256 / 链接目标 | 否, 安装器会渲染部分配置, 仅列出 |

输出条目: `CubeSandbox 结论` (一致 / 不一致, 说明中给出各类差异数)、对比基准、
安装目录、部署版本 (release-manifest.json 与 VERSION.txt)、每个组件一行
(`组件 Cubelet  一致 (二进制 3/3)`, 差异文件列在说明中)、每个运行中的基准二进制一行、
每个容器一行。未部署时只输出一行 `CubeSandbox 结论  N.A.`。

- 需对约 700 MB 安装文件计算 SHA256, 该模块的探测超时放宽到 180 秒 (实测 arm64 部署机约 7 秒);
- 非 root 时无法读取其它用户进程的 exe, 运行进程项为 `N.A.`;
- 无 docker 时容器项为 `N.A.` 并给出期望的镜像引用与 image ID, 结论中注明"容器未核对";
- 升级基准版本: 用 cube-perf-bench `cpb.py release-ref` 生成新的 `release/<ver>-<arch>.json`,
  拷入 `refs/` 并更新 `refs/cubesandbox-<ver>.json` (容器 image ID 与各架构清单文件名),
  再改 `modules/cubesandbox.py` 的 `INDEX_FILE`。

## Cube 运行资源配置（独立脚本 / cube-runtime 模块）

`cube_runtime.py` 专门检查 **CubeMaster、Cubelet、CubeAPI**，与原来的社区版本比较分开。
输出聚焦资源项：Go 参数、worker 数、CPU/NUMA、内存限制、文件句柄和应用并发。
网络地址/域名、shell 环境、日志/回溯、配置日志路径及其他非资源环境变量不再输出，
也不保留这些变量名称或 `<redacted>` 占位；表格与 JSON 使用相同的资源白名单。
只读采集，不重启服务、不执行配置文件、不写 sysctl/cgroup、不创建沙箱。
目标 Linux 机器需有 Python ≥ 3.8（标准库即可）；未安装时明确报告探测不可用。

```bash
# 独立脚本：终端表格 / 资源证据结构化 JSON / Markdown
python3 cube_runtime.py -H 192.168.25.90
python3 cube_runtime.py -H 192.168.25.90 -f json > cube-runtime.json
python3 cube_runtime.py -H 192.168.25.90 -f markdown > cube-runtime.md
sudo python3 cube_runtime.py --local
python3 cube_runtime.py -H 192.0.2.10,192.0.2.11 --ask-pass

# 集成到主工具：单查运行配置，或与版本比较一起输出
python3 machconf.py -H 192.168.25.90 -m cube-runtime
python3 machconf.py -H 192.168.25.90 -m cube,cube-runtime -f markdown
```

默认 `-m all` 也包含 `cube-runtime`。两种入口复用同一采集器，支持原有 table / markdown /
json / csv / csv-wide 格式。独立脚本的 JSON 保留原始结构（services / processes / cgroups），
主工具 JSON 遵循已有模块的 key / value / note 格式。`--cube-root` 用于非默认安装目录。

| 层级 | 输出内容 | 如何理解 |
| --- | --- | --- |
| systemd | 合并状态与 MainPID、NeedDaemonReload、资源环境变量及其来源、CPU/NUMA/内存/NOFILE 设置 | manager 当前加载的配置；不能直接当作运行进程实际限制 |
| 运行进程 | exe 身份、PID 来源、资源环境变量、各线程 CPU / NUMA allowed list | `/proc` 实测；PID 身份读取前后核对，消失/换代标明不可用 |
| cgroup | v1 CPU/cpuset/memory 或 v2 资源限制，及可读祖先层级 | 表格同一层合并一行；JSON 保留结构。不把单层限额当作全链路有效限额 |
| 应用文件 | 默认安装目录内并发数、资源开销等数字项白名单摘录，附行号/section | 仅磁盘配置；未证明进程已加载，不是完整 TOML/YAML 解析 |

环境值白名单：Go 的 `GOMAXPROCS`、`GOGC`、`GOMEMLIMIT`、`GODEBUG`；
CubeAPI / Rust 的 `WORKER_THREADS`、`TOKIO_WORKER_THREADS`。其他环境变量在目标机采集端直接过滤。
CubeAPI v0.7.1 使用 Rust、主要从环境读取配置，不虚构 `conf.yaml` 文件或推断未设置项的默认值。
`PassEnvironment` / `UnsetEnvironment` 仅保留资源白名单条目，其中带值的条目仍脱敏。
不输出完整命令行、ExecStart 或服务完整环境，不读取密钥文件。

`Environment` 只代表直接配置；`EnvironmentFiles` 列文件引用，不执行或展开文件内容。
EnvironmentFile、启动脚本、继承环境以及配置变更后尚未重启都可能导致 systemd 与进程环境不同。
白名单变量分别展示；仅对 systemd MainPID 的匹配进程作字面比较，同名扫描发现的其他实例不与该服务混比。变量缺失仅说明未在该来源读到，不推断 Go 默认值。
`/proc/PID/environ` 不是 Go 内部实时指标，程序通过 API 调整参数不会由本工具确认。

例如 `.90` 曾显示 systemd `AllowedCPUs=96-191`，而实际 v1 cpuset 与全部线程为 `0-383`。
本工具分别输出这些证据，不仅依据 drop-in 文件或 systemd 属性宣称已绑核。
NUMA allowed list 也不是应用实际内存页分布。

非 root、服务不存在、systemctl 不可用、进程退出等情况均保留状态/错误，不能将缺失当作
“默认配置”或“检查通过”。独立脚本遇到 SSH/整体探测失败退出 1；单项不可用保留在报告中，
退出 0 不代表所有服务健康。缺少服务时仍可按可执行文件名发现同名进程，并注明来源与身份。

相关 systemd 语义参见 [systemd.exec 官方文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。

## 按配置修改 Go 参数与 CPU / NUMA 亲和性

`cube_tune.py` 是独立的配置驱动入口，读取本地 JSON，通过 SSH 或 `--local` 操作目标 Linux。
**无内置调优配方、无默认 CPU/NUMA/Go 参数**；必须显式指定 `--config`。
目标机需要 Python ≥3.8、systemd；容器配置需要 Docker。`apply` / `restore` 需要目标机 root。

可编辑示例在 [`examples/cube-tune.json`](examples/cube-tune.json)。其中 CPU、节点和参数只是语法示例，
不是 `.90` 或其他机器的推荐值；请根据 `machconf.py -m numa,hardware,cube-runtime` 的结果填写。
服务名、容器名也由配置指定，可以增加其他组件。CubeAPI v0.7.1 是 Rust，只配置亲和性；
Go 环境参数必须显式声明 `runtime: "go"`。

默认示例中 Cubelet 仅列出 Go 参数，不设置 CPU / NUMA 亲和性；如需为 Cubelet 绑核或绑定 NUMA，
请在自己的配置文件中显式添加相应字段。

```bash
cp examples/cube-tune.json /tmp/my-cube-tune.json
# 编辑 /tmp/my-cube-tune.json 后预览；不写远端文件、不重启服务
python3 cube_tune.py plan -H 192.168.25.90 --config /tmp/my-cube-tune.json

# 应用，输出目标机 state_dir；默认服务仅写配置并 daemon-reload
python3 cube_tune.py apply -H 192.168.25.90 --config /tmp/my-cube-tune.json

# 检查实际运行进程；未重启的服务可能仍运行旧参数，此时验证不通过
python3 cube_tune.py verify -H 192.168.25.90 --config /tmp/my-cube-tune.json
# 也可按快照验证，并检查容器仍是原对象
python3 cube_tune.py verify -H 192.168.25.90 --state-dir /var/lib/machconf/cube-tune/实际快照目录

# 恢复该次操作前的受管文件和容器 cpuset
python3 cube_tune.py restore -H 192.168.25.90 --state-dir /var/lib/machconf/cube-tune/实际快照目录
```

省略动作默认为 `plan`。每次只操作一个主机，支持 `-H user@host`、`--ask-pass`；输出 JSON，
操作/验证失败退出 1。`apply --state-dir /绝对路径` 可指定尚不存在的目标机快照目录。

| 配置字段 | 说明 |
| --- | --- |
| `version` | 必须为 `1` |
| `services[].unit` | 精确 systemd `.service` 名称，不接受别名或未实例化模板 |
| `runtime` / `go_env` | `go` / `rust` / `other`；Go 支持 `GOMAXPROCS`、`GOGC`、`GOMEMLIMIT`、`GOTRACEBACK`、`GODEBUG`，值均为字符串 |
| `cpu_affinity` | CPU 列表，如 `0-7,16-23`，使用 `CPUAffinity`，并检查实际所有线程 |
| `numa_policy` / `numa_nodes` | `bind` / `interleave` / `preferred` 加节点列表；`preferred` 单节点；`default` / `local` 不接受节点列表 |
| `restart` | 默认 `false`，只有显式 `true` 才重启该服务；按服务列表顺序执行 |
| `containers[].name` | 精确的运行中 Docker 容器名称 |
| `cpuset_cpus` / `cpuset_mems` | Docker CPU / 内存允许节点列表，允许只指定一项 |

任意目标至少提供一项设置；未知字段、重复 JSON 字段、重复目标、非法值或不存在/离线 CPU、节点会报错。
服务写入独立的 `zzzz-machconf-tune.conf` drop-in，Go 参数同时写入同目录0600权限的
`zzzz-machconf-tune.env`，在已有 `EnvironmentFile` 之后加载，保留其他环境变量。
工具不更改原有配置文件。每个列出的服务都以该条配置完整替换
本工具的 drop-in：删除字段会移除上一次本工具设置的该字段，随后继承其他 systemd 配置；
移除 `go_env` 时会清空受管环境文件的旧参数并移除引用；从配置中删除整个目标则不再触碰它，
不自动清理旧设置。需要撤销整次应用时用 `restore`。
工具检查受管环境文件确实最后加载；与请求参数冲突的 `UnsetEnvironment` 会在写入前拒绝。
工具不会编辑启动脚本或原有环境文件；启动脚本仍可能覆盖环境，需要实际进程验证。

服务默认只加载新配置，报告“待重启”；后续由用户重启或在配置中显式启用重启。
Docker cpuset 在 `apply` 时立即更新，不需重启；这里只管理现有 Docker 容器，
不修改 Compose、镜像、容器环境或其他容器运行时，容器重建后需重新应用配置。

验证区分 systemd 加载值与实际进程：Go 只证明启动环境，无法证明程序内部 API 没有覆盖；
线程 CPU/容器节点允许集须为请求集合的非空子集，已有 cgroup 限制可能进一步缩小范围。
NUMA 检查主线程 `numa_maps` 中可观察的 VMA 策略；不把 `Mems_allowed_list` 当作内存策略，
也不承诺所有线程默认策略或已有页面迁移。应用自设 VMA 策略会导致严格检查不通过。

应用前保存私有快照（目录0700、文件0600）；写入失败会尝试恢复已变更目标。
应用/恢复使用主机级互斥锁，恢复拒绝覆盖已被外部修改的受管文件或替换的容器。
恢复仅重启本次曾显式重启的服务；不恢复进程 PID、既有内存布局或应用业务状态。
若连接中断或进程被杀，先查看指定/输出目录中的 `state.json`，再按该快照恢复；
遇到恢复错误应检查报告，不应把“已尝试恢复”当作成功。

## RCU 配置与检查

`python3 machconf.py -H 192.168.25.90 -m rcu` 检查运行时 RCU 开关和当前启动参数；
`-m all` 也包含此模块。配置沿用 `cube_tune.py` 的 JSON、预览、验证和快照恢复流程。
默认的组件配置不设置 RCU，Cubelet 默认也不添加 CPU / NUMA 亲和性。

依据 c100n100 性能调优工作区的 `reproduce/README.zh-CN.md`（实验记录不随本仓库发布）
“④ 开启 rcu_expedited”及 s13 恢复流程，本工具管理以下两个**运行时**接口：

| 配置项 | 接口 | 含义 |
| --- | --- | --- |
| `rcu.rcu_expedited` | `/sys/kernel/rcu_expedited` | `1` 请求使用加速宽限期路径 |
| `rcu.rcu_normal` | `/sys/kernel/rcu_normal` | `1` 强制普通宽限期，优先于 expedited |

两项都为 `0` 表示不通过这两个开关强制模式，**不表示禁用 RCU**，也不保证内核所有调用都不走加速路径。
查询同时观察 `/proc/cmdline` 中的 `rcupdate.rcu_expedited`、`rcupdate.rcu_normal`、
`rcupdate.rcu_normal_after_boot`；启动参数存在不证明当前运行值，未设置也不推断默认值。
接口不可读、缺失或值无法解析时明确报告不可用。相关语义参见
[Linux 内核参数文档](https://www.kernel.org/doc/html/latest/admin-guide/kernel-parameters.html)。

独立示例 [`examples/rcu.json`](examples/rcu.json) 为：

```json
{
  "version": 1,
  "rcu": {
    "rcu_expedited": 0,
    "rcu_normal": 0
  }
}
```

也可将 `rcu` 对象加入已有的服务/容器配置。两个值必须同时显式提供，且为 JSON 整数 `0` 或 `1`；
不接受缺项、布尔值、未知参数或含义冲突的 `1/1`。
若要测试指南的加速组合，将 `rcu_expedited` 改为 `1`、保持 `rcu_normal=0`，再预览。
指南及后续实验显示效果依赖测试条件，工具不会把它固化为默认优化。

```bash
# 只读查询
python3 machconf.py -H 192.168.25.90 -m rcu -f json
# 编辑自己的配置文件，预览 before/request 差异
python3 cube_tune.py plan -H 192.168.25.90 --config examples/rcu.json
# 显式应用：预检两个接口、保存旧值、按顺序写入并回读
python3 cube_tune.py apply -H 192.168.25.90 --config examples/rcu.json
python3 cube_tune.py verify -H 192.168.25.90 --config examples/rcu.json
# 使用 apply 返回的目标机快照目录
python3 cube_tune.py restore -H 192.168.25.90 --state-dir /var/lib/machconf/cube-tune/实际快照目录
```

RCU-only 配置不调用 systemd/Docker，不重启组件。切换到加速模式时先关闭 normal，再设置 expedited；
切换到强制普通模式时先关闭 expedited。失败会尝试恢复已修改项，并报告恢复结果。
RCU 快照记录 boot ID，拒绝跨主机重启恢复；缺失接口、非 `0/1` 运行值或已知不可写接口会阻止应用。
与服务/容器混合应用时，共用快照和互斥锁。

本功能不修改 GRUB、sysctl、tmpfiles 或其他开机配置，重启后应重新检查、按需应用。
指南中的持久化示例只处理 expedited，且没有完成 `.90` 重启验证，因此未自动安装；
若已有外部任务定期写这些开关，后续仍可能覆盖运行值，应在每轮实验前后执行查询或 `verify`。

## 本机模式与远程模式

两种模式走同一套模块与探测命令 (`bash -s`): 远程模式经 `ssh root@<host>` 执行,
本机模式 (`--local`) 直接在本机执行, 输出与远程模式一致 (表头为本机真实主机名)。
非 root 本机运行时, dmidecode 等需要特权的探测会优雅降级为 `N.A.` 并给出原因。

### 密码登录 (--ask-pass)

未配置免密的机器可加 `--ask-pass`: 密码由 ssh 自己在终端提示输入, 不经过本工具、
不进命令行参数或 shell 历史; 需在终端前台运行 (无终端的管道/定时任务场景请改用免密)。
同主机的连接会通过 ControlMaster 复用 1 分钟, 期间重复执行不再重复输密码。

## ARM KVM 启动参数与 GICv4 日志

`-m kvm-arm` 查询以下四项，默认 `-m all` 也包含此模块：

- `kvm-arm.vgic_v4_enable`
- `kvm-arm.virt_msi_bypass`
- `kvm-arm.dvmbm_enabled`
- `kvm-arm.ipiv_enabled`

分别展示当前 `/proc/cmdline`、`/etc/default/grub` 的静态摘录、grubby 默认内核启动项，
以及 dmesg 中的 KVM 状态和原始证据。参数精确匹配，重复/冲突值全部列出，不推断最后一项有效。
缺失参数不等于关闭；日志不可读、没有命中、状态冲突分别报告。
`ITS: Enabling GICv4 support` 是控制器层信息，不代替 `kvm [PID]: GICv4 support enabled/disabled`。
这些参数是否被内核实现和接受取决于目标内核，写入值不保证已经启用。

```bash
python3 machconf.py -H 192.168.25.90 -m kvm-arm

# 先预览：只读，不生成备份或修改文件
python3 machconf.py -H 192.168.25.90 \
  --set-kvm-arm vgic_v4_enable=1 \
  --set-kvm-arm virt_msi_bypass=1 \
  --set-kvm-arm dvmbm_enabled=1 \
  --set-kvm-arm ipiv_enabled=1 --dry-run

# 更新默认内核启动项（不会自动重启）
python3 machconf.py -H 192.0.2.10 --set-kvm-arm vgic_v4_enable=1
# 关闭指定项；也接受完整参数名
sudo python3 machconf.py --local --set-kvm-arm kvm-arm.ipiv_enabled=0
```

修改端要求目标机 ARM64、root、Python ≥3.8、grubby、grub2-script-check（或 grub-script-check）。
当前支持通过 `/etc/grub2-efi.cfg` 或 `/etc/grub2.cfg` 指向的完整、非 BLS GRUB 配置，
并要求同目录常规 `grubenv`。不支持的后端、含动态变量/复杂引用的目标参数、一个默认内核对应多个启动项，
都会明确拒绝，不猜测路径或重建配置。

每个 `--set-kvm-arm KEY=0|1` 只修改一个白名单键，可重复选项设置多个不同键；请求内重复键报错，
启动项内被修改键的重复值会去重。只更新**默认内核的唯一启动项**，可能与当前运行内核不同。
此操作不改 `/etc/default/grub`，保留旧内核各自参数；未来执行 `grub2-mkconfig` 可能覆盖这些修改。

写入时在目标 GRUB 目录生成 `.machconf-kvm-arm-<时间>-<随机值>/` 备份目录，保留 `original.cfg`
与 `plan.json`。先在副本执行 grubby、检查语法、验证默认项/其他启动项/无关参数和 grubenv，
确认只有一行 Linux 启动参数变化后原子替换，再回读验证；安装后验证失败会尝试回滚并报告结果。
发生并发配置变化时拒绝覆盖。备份路径始终显示在已生成备份的操作结果中。
修改输出为操作日志，不受 `-f` 控制，不能与 NUMA/THP/HugeTLB 修改组合。

成功只表示**下次默认启动项已配置，待重启验证**；工具不重启，也不直接写 `/proc/cmdline`。
机器正常重启后再次运行 `-m kvm-arm`，分别核对启动参数与 KVM 日志。
引导项修改方式参见 [Red Hat grubby 文档](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/8/html/managing_monitoring_and_updating_the_kernel/assembly_making-persistent-changes-to-the-grub-boot-loader_managing-monitoring-and-updating-the-kernel)。

## 大页运行时修改

需在目标机拥有 root/对应 sysfs 写权限，支持本机和 SSH 多主机。仅修改运行时配置，
不写启动参数、sysctl 持久化文件或 systemd unit。重启后由机器原有配置决定。

```bash
# 清除所有页尺寸的 HugeTLB 静态大页池
sudo python3 machconf.py --local --clear-hugepages
python3 machconf.py -H 192.0.2.10,192.0.2.11 --clear-hugepages

# THP 模式：never / madvise / always
python3 machconf.py -H 192.0.2.10 --set-thp never
sudo python3 machconf.py --local --set-thp madvise

# 同次请求先设置 THP，再清除 HugeTLB
python3 machconf.py -H 192.0.2.10 --set-thp never --clear-hugepages
```

- `--clear-hugepages` 遍历 `/sys/kernel/mm/hugepages/hugepages-*kB`，将各尺寸
  `nr_hugepages` 置零，并报告原值及回读的 `nr_hugepages`、`free_hugepages`、
  `resv_hugepages`、`surplus_hugepages`。任一计数非零或不可读，均报告未完全清除，
  返回非零退出码。占用中的页可能转为 surplus，需释放使用者后重试；工具不停止进程。
  不修改 `nr_overcommit_hugepages`，因此这不是永久禁止后续 HugeTLB 分配。
- `--set-thp` 同时设置顶层 `enabled` 和内核导出的各尺寸匿名 THP `enabled`，
  全部使用请求的模式并逐项回读。`always` / `madvise` 也会覆盖原来的尺寸策略，
  因而可以重新启用之前设为 `never` 的尺寸；不是只恢复内核默认的 PMD 尺寸。
  不修改 `defrag`、shmem/tmpfs 策略，不立即拆分已经存在的大页；`MADV_COLLAPSE`
  仍可绕过这些开关。参考 [内核 THP 文档](https://docs.kernel.org/admin-guide/mm/transhuge.html)。
- 两种修改均先预检各自全部接口。执行期间仍可能部分成功，失败不自动回滚；
  输出保留各项结果，任一主机/操作失败则整体退出 1。输出为操作日志，不受 `-f` 控制，
  不执行 `-m` 查询。`--set-numa` 需单独执行。

HugeTLB 回收语义参见 [内核 HugeTLB 文档](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html)。

## numa_balancing.sh

同目录下的 `numa_balancing.sh` 是独立的单机版 (查询/修改/读回验证), 可直接在目标机
上执行; `numa` 模块的查询逻辑与它保持一致。

## 扩展新模块

1. 在 `modules/` 下新建文件, 实现 `Module` 子类 (`name/title/group`, `probes()` 返回
   `[(tag, command)]`, `parse(sections)` 返回 `[Item]`; 命令需自带 `|| true` 容错);
2. 在 `modules/__init__.py` 的 `_ORDER` 与 `_build_registry()` 注册;
3. 在 `tests/fixtures/` 放真实探测输出, 为 `parse()` 补充解析测试。

所有模块的探测会合并为一次 SSH 会话执行, 新增模块不增加连接开销。

## 测试

```bash
cd tools/machconf
python3 -m unittest discover -v
```

fixture 来自鲲鹏 950 / openEuler 24.03 (aarch64) 真实输出与 x86 合成输出;
`cubesandbox-v071-arm64` 为社区 v0.7.1 arm64 一键部署机器上 cube 模块的真实探测输出
(docker 镜像列表只保留 CubeSandbox 组件)。
