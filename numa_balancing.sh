#!/usr/bin/env bash
#
# numa_balancing.sh — 查询/修改内核 NUMA 自动均衡运行时开关 (kernel.numa_balancing)
#
# 用法:
#   ./numa_balancing.sh              # 等价于 status
#   ./numa_balancing.sh status       # 查询当前状态(人类可读)
#   ./numa_balancing.sh get          # 仅输出原始值 0/1, 便于脚本调用
#   ./numa_balancing.sh enable       # 开启 (等价 set 1)
#   ./numa_balancing.sh disable      # 关闭 (等价 set 0)
#   ./numa_balancing.sh set <0|1>    # 修改为指定值 (需 root)
#   ./numa_balancing.sh help         # 显示帮助
#
# 退出码: 0=成功  1=执行失败  2=用法错误
#
# 说明: 修改仅对运行时生效, 重启后恢复; 永久生效需写入 /etc/sysctl.conf:
#   echo "kernel.numa_balancing = 0" >> /etc/sysctl.conf && sysctl -p
#
# 该脚本也可被 source, 直接调用函数接口:
#   source numa_balancing.sh
#   get_numa_balancing        # 输出 0/1
#   set_numa_balancing 0      # 修改, 返回码表示成败

set -u

PROC_PATH="/proc/sys/kernel/numa_balancing"

# ---------- 函数接口 ----------

# 输出当前值 (0/1); 不支持时输出空且返回 1
get_numa_balancing() {
    if [[ ! -f "$PROC_PATH" ]]; then
        echo "numa_balancing: 内核不支持或未启用 NUMA ($PROC_PATH 不存在)" >&2
        return 1
    fi
    cat "$PROC_PATH"
}

# 修改当前值; 参数: 0|1|on|off|enable|disable; 成功返回 0
set_numa_balancing() {
    local value
    case "${1:-}" in
        1|on|enable)  value=1 ;;
        0|off|disable) value=0 ;;
        *) echo "numa_balancing: 无效参数 '$1' (仅支持 0/1/on/off/enable/disable)" >&2; return 2 ;;
    esac

    if [[ ! -f "$PROC_PATH" ]]; then
        echo "numa_balancing: 内核不支持或未启用 NUMA ($PROC_PATH 不存在)" >&2
        return 1
    fi
    if [[ $EUID -ne 0 ]]; then
        echo "numa_balancing: 修改需要 root 权限 (try sudo)" >&2
        return 1
    fi

    local old
    old=$(cat "$PROC_PATH")
    if [[ "$old" == "$value" ]]; then
        echo "numa_balancing: 已经是 $value, 无需修改" >&2
        return 0
    fi

    if ! echo "$value" > "$PROC_PATH" 2>/dev/null; then
        echo "numa_balancing: 写入 $PROC_PATH 失败" >&2
        return 1
    fi

    # 读回验证
    local now
    now=$(cat "$PROC_PATH")
    if [[ "$now" != "$value" ]]; then
        echo "numa_balancing: 写入后读回为 $now, 与预期 $value 不符" >&2
        return 1
    fi
    echo "numa_balancing: $old -> $value" >&2
    return 0
}

# 人类可读的状态输出
show_status() {
    local value
    if ! value=$(get_numa_balancing); then
        return 1
    fi
    value=$(echo "$value" | tr -d '[:space:]')

    local desc
    if   [[ "$value" == "1" ]]; then desc="开启 (内核自动在 NUMA 节点间迁移页)"
    elif [[ "$value" == "0" ]]; then desc="关闭"
    else desc="未知值"
    fi

    echo "kernel.numa_balancing = $value  ($desc)"
    echo "配置路径: $PROC_PATH"

    # 内核启动参数(优先级更高, 会覆盖 sysctl 默认值)
    local cmdline
    cmdline=$(cat /proc/cmdline 2>/dev/null | grep -o 'numa_balancing=[a-z]*' || true)
    [[ -n "$cmdline" ]] && echo "注意: 内核启动参数中存在 $cmdline (重启后以其为准)"

    # 是否有持久化配置
    if [[ -f /etc/sysctl.conf ]] && grep -qs '^\s*kernel.numa_balancing' /etc/sysctl.conf; then
        grep '^\s*kernel.numa_balancing' /etc/sysctl.conf | sed 's/^/持久化配置: /'
    fi
    return 0
}

usage() {
    sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
}

# ---------- CLI 入口 (source 时不执行) ----------

if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0 2>/dev/null || true
fi

main() {
    local cmd="${1:-status}"
    case "$cmd" in
        status)  show_status ;;
        get)     get_numa_balancing ;;
        enable)  set_numa_balancing 1 ;;
        disable) set_numa_balancing 0 ;;
        set)
            [[ $# -ge 2 ]] || { echo "用法: $0 set <0|1>" >&2; exit 2; }
            set_numa_balancing "$2"
            ;;
        help|-h|--help) usage ;;
        *) echo "未知命令: $cmd (试试 help)" >&2; exit 2 ;;
    esac
}

main "$@"
