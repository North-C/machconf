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

# 单模块 (bios / os / numa / hardware / cube)
python3 machconf.py --local -m numa
python3 machconf.py -H 192.0.2.10 -m numa

# CubeSandbox 组件对比社区 v0.7.1 (需 root; 非默认安装目录用 --cube-root)
python3 machconf.py -H 192.0.2.10 -m cube
python3 machconf.py -H 192.0.2.10,192.0.2.11 -m cube -f csv-wide > cube-compare.csv
sudo python3 machconf.py --local -m cube --cube-root /opt/cubetoolbox

# 多台汇总
python3 machconf.py -H 192.0.2.10,192.0.2.11

# 输出格式: table (默认) / json / markdown / csv / csv-wide
python3 machconf.py -H 192.0.2.10 -f markdown > report.md
python3 machconf.py -H 192.0.2.10 -f csv > report.csv           # CSV 长表 (Excel/pandas)
python3 machconf.py -H 192.0.2.10,192.0.2.11 -f csv-wide > compare.csv  # 宽表多机对比

# 修改 numa_balancing (立即生效, 回读验证; 重启后恢复)
sudo python3 machconf.py --local --set-numa 0
python3 machconf.py -H 192.0.2.10 --set-numa 0
```

## 模块

| 模块 | 内容 | 主要数据源 |
| --- | --- | --- |
| `bios` | BIOS 固件/整机型号、CPU 核心×路×线程 (SMT)、NUMA 节点及各节点 CPU 分布、governor/C-state/EPB (功耗)、睿频/超频 | dmidecode, lscpu, cpufreq sysfs |
| `os` | 发行版/内核版本、静态大页 (容量/空闲/各 NUMA 节点分布)、透明大页 THP | /proc/meminfo, /sys/devices/system/node |
| `numa` | `kernel.numa_balancing` 运行时开关、内核启动参数覆盖、sysctl 持久化配置 | /proc/sys, /proc/cmdline, sysctl.conf |
| `hardware` | 内存容量/类型/频率 (实配与标称)、磁盘大小/类型 (NVMe/SSD/HDD)/角色 (系统盘/数据盘/未挂载) | dmidecode, lsblk |
| `cube` | CubeSandbox 各组件文件、运行进程、容器镜像与社区 v0.7.1 离线包是否一致 | sha256sum, /proc/\<pid\>/exe, docker |

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

## 本机模式与远程模式

两种模式走同一套模块与探测命令 (`bash -s`): 远程模式经 `ssh root@<host>` 执行,
本机模式 (`--local`) 直接在本机执行, 输出与远程模式一致 (表头为本机真实主机名)。
非 root 本机运行时, dmidecode 等需要特权的探测会优雅降级为 `N.A.` 并给出原因。

### 密码登录 (--ask-pass)

未配置免密的机器可加 `--ask-pass`: 密码由 ssh 自己在终端提示输入, 不经过本工具、
不进命令行参数或 shell 历史; 需在终端前台运行 (无终端的管道/定时任务场景请改用免密)。
同主机的连接会通过 ControlMaster 复用 1 分钟, 期间重复执行不再重复输密码。

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
python3 -m unittest test_machconf -v
```

fixture 来自鲲鹏 950 / openEuler 24.03 (aarch64) 真实输出与 x86 合成输出;
`cubesandbox-v071-arm64` 为社区 v0.7.1 arm64 一键部署机器上 cube 模块的真实探测输出
(docker 镜像列表只保留 CubeSandbox 组件)。
