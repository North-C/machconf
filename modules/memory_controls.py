"""运行时大页修改脚本；本机和 SSH 共用，只依赖目标机 bash/sysfs。"""

import shlex


THP_MODES = ("always", "madvise", "never")
STATUS_PREFIX = "@@MC_STATUS:"

# 修改失败也保留逐项回读结果；最终标记由调用方转换为 CLI 退出码。
_HELPERS = r'''
failed=0
error() { printf 'ERROR %s\n' "$*"; failed=1; }
finish() { printf '@@MC_STATUS:%s@@\n' "$failed"; exit 0; }
number() {
    local value
    value=$(cat "$1" 2>/dev/null) || return 1
    case "$value" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s' "$value"
}
mode() {
    local value
    value=$(cat "$1" 2>/dev/null) || return 1
    case "$value" in
        *'['*']'*) value=${value#*[}; value=${value%%]*} ;;
        *) return 1 ;;
    esac
    case "$value" in always|madvise|never|inherit) printf '%s' "$value" ;; *) return 1 ;; esac
}
'''


def clear_hugepages_command(root="/sys/kernel/mm/hugepages"):
    """所有尺寸的持久池置零，检查占用转为 surplus 等未释放状态。"""
    return _HELPERS + "hp_root=%s\n" % shlex.quote(str(root)) + r'''
shopt -s nullglob
dirs=("$hp_root"/hugepages-*kB)
if [ "${#dirs[@]}" -eq 0 ]; then
    error "HugeTLB 不可用: $hp_root 下没有页尺寸接口"
    finish
fi
# 全量预检，避免已知不可读/不可写时只修改部分尺寸。
for dir in "${dirs[@]}"; do
    [ -w "$dir/nr_hugepages" ] || error "$dir/nr_hugepages 不可写 (需要 root/可写 sysfs)"
    for key in nr_hugepages free_hugepages resv_hugepages surplus_hugepages; do
        number "$dir/$key" >/dev/null || error "$dir/$key 不可读或不是数字"
    done
done
[ "$failed" -eq 0 ] || finish
for dir in "${dirs[@]}"; do
    old=$(number "$dir/nr_hugepages") || { error "$dir: 修改前读取失败"; continue; }
    if ! printf '0\n' > "$dir/nr_hugepages"; then
        error "$dir: 写入失败"
    fi
    summary=""
    remaining=0
    for key in nr_hugepages free_hugepages resv_hugepages surplus_hugepages; do
        if value=$(number "$dir/$key"); then
            [ "$value" -eq 0 ] || remaining=1
        else
            value="N.A."
            remaining=1
        fi
        summary="$summary $key=$value"
    done
    printf '%s: 原 nr_hugepages=%s;%s\n' "$dir" "$old" "$summary"
    if [ "$remaining" -ne 0 ]; then
        error "$dir: 未完全清除 (仍有占用/预留/剩余页，或回读失败)；释放使用者后重试"
    fi
done
finish
'''


def set_thp_command(value, root="/sys/kernel/mm/transparent_hugepage"):
    """统一设置顶层及各尺寸匿名 THP enabled；不调整 shmem/defrag。"""
    if value not in THP_MODES:
        raise ValueError("不支持的 THP 模式: %s" % value)
    return _HELPERS + "thp_root=%s\nrequested=%s\n" % (
        shlex.quote(str(root)), shlex.quote(value)) + r'''
shopt -s nullglob
files=("$thp_root/enabled" "$thp_root"/hugepages-*kB/enabled)
for file in "${files[@]}"; do
    [ -w "$file" ] || error "$file 不可写/不存在 (需要 root/内核 THP 支持)"
    mode "$file" >/dev/null || error "$file: 当前模式不可读"
    options=$(cat "$file" 2>/dev/null) || options=""
    options=${options//[/ }
    options=${options//]/ }
    supported=0
    for option in $options; do
        [ "$option" != "$requested" ] || supported=1
    done
    [ "$supported" -eq 1 ] || error "$file 不支持模式 $requested"
done
[ "$failed" -eq 0 ] || finish
for file in "${files[@]}"; do
    old=$(mode "$file") || { error "$file: 修改前读取失败"; continue; }
    if [ "$old" != "$requested" ]; then
        if ! printf '%s\n' "$requested" > "$file"; then
            error "$file: 写入失败"
        fi
    fi
    now=$(mode "$file") || now="N.A."
    printf '%s: %s -> %s (期望 %s)\n' "$file" "$old" "$now" "$requested"
    [ "$now" = "$requested" ] || error "$file: 回读与期望不符"
done
finish
'''


def parse_change_output(output):
    """缺失完成标记视为失败，不能把空输出当作修改成功。"""
    lines = output.strip().splitlines()
    if lines and lines[-1] in (STATUS_PREFIX + "0@@", STATUS_PREFIX + "1@@"):
        return lines[-1] == STATUS_PREFIX + "0@@", lines[:-1]
    return False, lines + ["ERROR 未收到完整修改结果，状态未知，请重新查询"]
