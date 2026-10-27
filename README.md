# 📡 Ultrasonic Distance Perception Demo - Quectel Pi M2

English | [中文](README_zh.md)

This project is developed based on the [Quectel Pi M2 Smart Single-Board Computer](https://www.quectel.com.cn/product/quectel-pi-m2).
A rainbow RGB ultrasonic module ranges over the on-board **I2C7** bus, and a servo driven by **hardware PWM**
sweeps the probe through 0–180°; the readings of a full sweep are grouped into **left / centre / right** sectors
to tell you *which side* the nearest object is on, rendered live in the GUI as a top-down polar map and a
distance history.

![Interface preview](assets/rendering.jpg)

## 🎯 Key Features

- **A direction result, not just a distance**: every sweep is grouped per sector and the answer is exactly one of `LEFT` / `CENTER` / `RIGHT` — stable, never flickering
- **Top-down polar scan map**: the centre is the rotating probe (R/T transducers + two RGB LEDs), the upper half-circle is the servo travel with `LEFT` / `CENTER` / `RIGHT` sectors, and the nearest point is highlighted with its distance
- **Two scan modes**: a single sweep (default, parks the servo afterwards) and continuous sweeping, with a 5–45° step
- **Motion smoothing**: the servo follows a trapezoidal velocity profile instead of stepping to the target, so large moves produce no gear chatter and no current spikes
- **Graceful dropout handling**: the module drops 5–7 % of reads, so the console holds the previous value (dimmed, marked "held") until `HOLD_FAILS` consecutive drops, and the history bridges short gaps with a dotted line
- **Runs without hardware**: `--sim` reproduces real timing *and* which direction the fake object is in, so the UI and the evaluation logic can be validated off-board
- **Permissions never crash it**: without PWM write access it falls back to simulation, keeps the whole UI usable and prints the exact command to fix it
- **Scanning is refused on "real sonar + fake servo"**: when the servo is not actually driven every angle reads the same direction, so automatic scanning is disabled instead of fabricating an answer
- **One dependency layer**: `PySide6==6.9.1` is all it needs at runtime; the I2C and PWM drivers use only the standard library (PWM is written straight to sysfs)

## 📋 System Requirements

### Hardware Requirements

- Quectel Pi M2 board (RK3576 · Debian · 40-pin header)
- Rainbow RGB ultrasonic module (STC8H1K08 + RCWL-9616, I2C `0x2D`, range 2–400 cm)
- TXS0108 bidirectional level shifter (8 channels, 2 used in this demo)
- SG90 / MG90S class servo (50 Hz, 500–2500 µs ↔ 0–180°)
- Buck regulator module with **multiple independent outputs** — one output feeds the board, another feeds the servo
- 7.4 V lithium battery (2S) as the input of the regulator
- A display (or an HDMI monitor) and a USB keyboard/mouse to operate the GUI
- Jumper wires

Wiring diagram: `assets/wiring.svg`. The servo must be **powered separately from the board with a common ground** — a stalled servo can draw 500–700 mA, and sharing one 5 V rail drags it down and knocks the ultrasonic module offline.

### Software Requirements

- Operating system: **Debian 13** (the factory image of the Quectel Pi M2 — nothing to reflash)
- Python 3, used through an isolated `python3 -m venv` environment
- Dependencies: `PySide6 == 6.9.1` (the only runtime dependency, see `requirements.txt`)
- Peripherals: `/dev/i2c-7` (I2C7, Pin27/Pin28) and `/sys/class/pwm` (PWM2_CH3, Pin33)
- Reading `/dev/i2c-7` needs group membership: add your user to the `i2c` group and log in again

## 🚀 Installation & Running

1. Clone the project on the board.

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/Quectel-Pi/demo-ultrasonic-sensing.git
```

After that a `demo-ultrasonic-sensing` folder appears in the current directory.

2. Enter the project and install the dependencies in an isolated virtual environment (`requirements.txt` holds a single entry, `PySide6==6.9.1`).

```bash
cd demo-ultrasonic-sensing
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
```

> The system `python3` on the board usually has no `pip` module and `sudo` needs a password, so **do not** try `sudo apt install python3-pip` — a `python3 -m venv` environment ships its own `pip`.

3. Grant the servo PWM access **once** (the kernel exposes the PWM channel through sysfs, which is writable by root only, so this needs the root password).

```bash
sudo bash deploy/setup_servo_pwm.sh
```

**Nothing breaks without it.** Without write access the program falls back to simulation: the scan flow and the direction result still demo correctly, the servo just does not move, and the panel shows the exact command to run.

4. Run the program.

```bash
cd demo-ultrasonic-sensing
python3 run_scan.py
```

### Enable Autostart at Boot (Optional)

```bash
cp deploy/demo-ultrasonic-sensing.desktop ~/.config/autostart/
# Exec and Path already point at ~/projects/demo-ultrasonic-sensing; edit those two lines if your path differs
```

## 🎛️ Usage

The scan view and the distance history sit on the left, the control console on the right.

- **Top-left — scan view**: the top-down polar map, where the upper half-circle is the servo travel split into `LEFT` / `CENTER` / `RIGHT` sectors, with the nearest point highlighted and labelled.
- **Bottom-left — history**: `Distance history (last 30 s)`, with short gaps bridged by a dotted line.
- **Right — control console**: only three group boxes stay visible (`Distance` / `Detection threshold` / `Rotate & Scan`); the rest fold behind clickable `▸ sections`.
- **`⟳ Rotate & Scan (180°)`** starts a single sweep (default 5° step, 37 sample angles, about 12.7 s) and parks the servo at 90° afterwards.
- **`Continuous scan (repeat)`** (or `--auto-scan`) sweeps back to back and refines the result every pass; unchecking it parks the probe.
- The result block `NEAREST OBJECT` reports `LEFT` / `CENTER` / `RIGHT`; every tunable parameter lives in `src/config.py`.

![Sweep in progress](assets/ui_scanning.png)

## 📁 Project Structure

```
demo-ultrasonic-sensing/
├── run_scan.py                  board launcher (picks an interpreter → starts the GUI)
├── requirements.txt             dependencies (PySide6 only)
├── README.md                    this file (English)
├── README_zh.md                 Chinese documentation
├── assets/
│   ├── wiring.svg               wiring diagram
│   ├── ui_main.png              screenshot: main window
│   └── ui_scanning.png          screenshot: sweep in progress
├── deploy/
│   ├── setup_servo_pwm.sh       one-time PWM permission (sudo)
│   └── demo-ultrasonic-sensing.desktop   desktop shortcut / autostart
└── src/
    ├── main.py                  entry point: CLI parsing + lazy PySide6 import
    ├── config.py                global configuration: the single source of every tunable
    ├── driver/                  hardware layer: I2C transport + US01 protocol, sysfs PWM servo
    ├── controller/              acquisition / scan state machine / direction result
    ├── ui/                      main window: layout / signals / theme
    └── widgets/                 polar scan view, distance history, collapsible sections
```

Imports only ever go one way: `widgets / ui → controller → driver → config`.

## 📚 More Documentation

Wiring and power notes, sweep timing and the direction algorithm, the full `src/config.py`
parameter tables, troubleshooting and design notes are published as this demo's resource
document on the Quectel developer documentation site.

## 📮 Reporting Issues

If you run into any problem, feel free to post a technical inquiry in the
[Quectel Pi developer forum](https://forumschinese.quectel.com/c/quectel-pi/58) — our support team answers there.

Issues and Pull Requests are welcome.
