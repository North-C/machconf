# machconf — 机器配置检测工具

通过 SSH 免密登录批量检测服务器的 BIOS / OS / NUMA / 硬件配置, 输出对齐表格、
JSON 或 Markdown 报告。面向性能测试前的机器基线核对 (NUMA 拓扑、大页、内存频率、
数据盘类型等), x86 与 ARM 机型均可使用, 平台不可用项优雅降级为 `N.A.` 并说明原因。

## 前置条件

- Python ≥ 3.8, 仅标准库
- 远程模式: 目标机 `ssh root@<host>` 免密可登录
- 本机模式 (`--local`): 无需 SSH; BIOS/内存等完整信息仍需 root (sudo) 运行

## 快速开始

```bash
# 本机检测, 无需 SSH
sudo python3 machconf.py --local

# 远程全模块检测, 表格输出
python3 machconf.py -H 192.0.2.10

# 单模块 (bios / os / numa / hardware)
python3 machconf.py --local -m numa
python3 machconf.py -H 192.0.2.10 -m numa

# 多台汇总
python3 machconf.py -H 192.0.2.10,192.0.2.11

# 输出格式: table (默认) / json / markdown
python3 machconf.py -H 192.0.2.10 -f markdown > report.md

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

说明:

- BIOS 菜单项无法在运行中的 OS 内直接读取 (需厂商带外工具), `bios` 模块检测的是
  BIOS 设置在 OS 侧的可见结果; 不同机型/架构可用项不同。
- 超频检测: x86 走 `intel_pstate/no_turbo`; ARM 等平台按最大频率与基础频率对比
  推断 (标注"推断")。
- 磁盘角色按挂载点判别, 逐盘 `lsblk` 聚合含 LVM 子设备的挂载点, 系统盘 = 挂载了
  `/`、`/boot` 或 swap 的盘, 其余已挂载盘为数据盘。

## 本机模式与远程模式

两种模式走同一套模块与探测命令 (`bash -s`): 远程模式经 `ssh root@<host>` 执行,
本机模式 (`--local`) 直接在本机执行, 输出与远程模式一致 (表头为本机真实主机名)。
非 root 本机运行时, dmidecode 等需要特权的探测会优雅降级为 `N.A.` 并给出原因。

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

fixture 来自鲲鹏 950 / openEuler 24.03 (aarch64) 真实输出与 x86 合成输出。
