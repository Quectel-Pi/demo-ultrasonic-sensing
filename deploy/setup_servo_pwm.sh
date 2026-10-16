#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 舵机 PWM 一次性授权（板载 Pin33 = PWM2_CH3 = 2ade3000.pwm）
#
# 为什么需要这一步
#   /sys/class/pwm/pwmchipN/export 与导出后的 pwmM/* 默认 **仅 root 可写**，
#   而"先 export 再 chmod"只有 root 能做（新导出的通道目录仍然是 root 所有）。
#   所以只需要一次 root 操作：导出通道 + 把权限放开给 dialout 组，
#   之后应用程序就能免 root 直接写 period/duty_cycle/enable。
#
# 用法（在板上，工程根目录）
#   sudo bash deploy/setup_servo_pwm.sh            # 授权 + 装开机自启（推荐）
#   sudo bash deploy/setup_servo_pwm.sh --now      # 只本次授权，不装开机自启
#   sudo bash deploy/setup_servo_pwm.sh --status   # 只看状态，不做任何改动
#   sudo bash deploy/setup_servo_pwm.sh --revert   # 卸载开机自启（不动通道权限）
#
# 设计要点
#   * 控制器按**设备名**查找，不写死 pwmchipN —— 重启后编号可能变。
#   * 开机自启用 systemd oneshot + 一条 udev 规则双保险：前者管正常启动，
#     后者管控制器后出现（热插拔/驱动重载）的情况。
#   * 通道**只导出不回收**：应用退出时也保持导出，否则下次又要 root 重新导出。
# ---------------------------------------------------------------------------
set -uo pipefail

CHIP_DEVICE="${PWM_CHIP_DEVICE:-2ade3000.pwm}"   # PWM2 控制器（Pin33 = CH3）
CHANNEL="${PWM_CHANNEL:-0}"                      # 该控制器只暴露 1 个通道
GROUP="${PWM_GROUP:-dialout}"                    # 放开给这个组（板端用户在 dialout）
HELPER="/usr/local/sbin/setup-servo-pwm.sh"      # 开机自启用的常驻副本
UNIT="/etc/systemd/system/servo-pwm-setup.service"
RULE="/etc/udev/rules.d/99-quectel-pwm.rules"
PWM_ROOT="/sys/class/pwm"

QUIET=0
MODE="install"
for arg in "$@"; do
    case "$arg" in
    --now)    MODE="now" ;;
    --status) MODE="status" ;;
    --revert) MODE="revert" ;;
    --quiet)  QUIET=1 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（试 --help）" >&2; exit 2 ;;
    esac
done

say() { [ "$QUIET" -eq 1 ] || echo "$@"; }
die() { echo "错误：$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 找控制器 / 通道
# ---------------------------------------------------------------------------
find_chip() {
    local c dev
    for c in "$PWM_ROOT"/pwmchip*; do
        [ -e "$c" ] || continue
        dev="$(readlink -f "$c/device" 2>/dev/null || true)"
        if [ "$(basename "$dev")" = "$CHIP_DEVICE" ]; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

show_status() {
    local chip ch
    echo "=== PWM 授权状态 ==="
    if ! chip="$(find_chip)"; then
        echo "  控制器 $CHIP_DEVICE ：未找到"
        echo "  已有的控制器："
        for c in "$PWM_ROOT"/pwmchip*; do
            [ -e "$c" ] || continue
            echo "    $c  npwm=$(cat "$c/npwm" 2>/dev/null)  device=$(readlink -f "$c/device" 2>/dev/null)"
        done
        return 1
    fi
    echo "  控制器 $CHIP_DEVICE ：$chip"
    ch="$chip/pwm$CHANNEL"
    if [ -d "$ch" ]; then
        echo "  通道 $ch ：已导出"
        ls -l "$ch" | sed 's/^/    /'
    else
        echo "  通道 $ch ：**未导出**（需要 sudo 运行本脚本）"
        return 1
    fi
    local f bad=0
    for f in period duty_cycle enable; do
        if [ -w "$ch/$f" ]; then
            echo "  可写 $f ：是"
        else
            echo "  可写 $f ：否  ← 应用会转仿真"
            bad=1
        fi
    done
    [ "$bad" -eq 0 ] && echo "  结论：应用可免 root 驱动舵机"
    return "$bad"
}

# ---------------------------------------------------------------------------
# 导出 + 放开权限（必须 root）
# ---------------------------------------------------------------------------
do_grant() {
    local chip ch f
    chip="$(find_chip)" || die "未找到 PWM 控制器 $CHIP_DEVICE（/sys/class/pwm 下无匹配）"
    say "控制器：$chip"

    ch="$chip/pwm$CHANNEL"
    if [ ! -d "$ch" ]; then
        say "导出通道 $CHANNEL …"
        echo "$CHANNEL" > "$chip/export" || die "写 $chip/export 失败"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            [ -d "$ch" ] && break
            sleep 0.05
        done
        [ -d "$ch" ] || die "export 后仍未出现 $ch（通道号可能超出 npwm=$(cat "$chip/npwm")）"
    else
        say "通道已导出：$ch"
    fi

    say "放开 $GROUP 组读写权限…"
    chgrp "$GROUP" "$chip" 2>/dev/null || true
    chmod 0775 "$chip" 2>/dev/null || true
    for f in export unexport; do
        [ -e "$chip/$f" ] || continue
        chgrp "$GROUP" "$chip/$f" 2>/dev/null || true
        chmod 0660 "$chip/$f" 2>/dev/null || true
    done
    chgrp "$GROUP" "$ch" 2>/dev/null || true
    chmod 0775 "$ch" 2>/dev/null || true
    for f in period duty_cycle enable polarity; do
        [ -e "$ch/$f" ] || continue
        chgrp "$GROUP" "$ch/$f" 2>/dev/null || true
        chmod 0660 "$ch/$f" 2>/dev/null || true
    done
    say "完成。"
}

# ---------------------------------------------------------------------------
# 装开机自启（systemd oneshot + udev 规则）
# ---------------------------------------------------------------------------
do_install() {
    say "安装常驻副本 $HELPER …"
    install -m 0755 "$0" "$HELPER"

    say "写 udev 规则 $RULE …"
    cat > "$RULE" <<EOF
# Quectel Pi M2 板载 PWM（Pin33 / 2ade3000.pwm）：导出通道并放开 $GROUP 组权限
# 由 ultrasonic_control 的 deploy/setup_servo_pwm.sh 安装
SUBSYSTEM=="pwm", KERNEL=="pwmchip*", ACTION=="add", RUN+="$HELPER --now --quiet"
EOF
    chmod 0644 "$RULE"

    say "写 systemd 单元 $UNIT …"
    cat > "$UNIT" <<EOF
[Unit]
Description=Export onboard PWM channel for servo and grant $GROUP access
Documentation=file:$HELPER
After=sysinit.target
Before=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$HELPER --now --quiet

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$UNIT"

    systemctl daemon-reload || true
    systemctl enable servo-pwm-setup.service >/dev/null 2>&1 \
        && say "已启用开机自启：servo-pwm-setup.service" \
        || say "警告：systemctl enable 失败，开机自启可能不生效"
    udevadm control --reload-rules >/dev/null 2>&1 || true
}

do_revert() {
    say "卸载开机自启…"
    systemctl disable servo-pwm-setup.service >/dev/null 2>&1 || true
    rm -f "$UNIT" "$RULE"
    systemctl daemon-reload || true
    udevadm control --reload-rules >/dev/null 2>&1 || true
    say "已移除 $UNIT 与 $RULE（PWM 通道权限保持不变）"
}

# ---------------------------------------------------------------------------
main() {
    case "$MODE" in
    status) show_status; exit $? ;;
    revert) [ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0 --revert"
            do_revert; exit 0 ;;
    now)    [ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0 --now"
            do_grant ;;
    install)
        [ "$(id -u)" -eq 0 ] || die "需要 root：sudo bash $0"
        do_grant
        do_install
        ;;
    esac

    echo
    if show_status; then
        echo
        echo "现在可以直接运行（无需 sudo）："
        echo "    cd ~/projects/demo-ultrasonic-sensing && ./run_scan.py"
    else
        echo
        echo "警告：授权后自检仍未通过，请把上面的输出发出来。"
        exit 1
    fi
}

main "$@"
