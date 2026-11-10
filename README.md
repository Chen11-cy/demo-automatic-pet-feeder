# Pet Feeder AI Demo (M2)

这是一个可直接运行的宠物自动投喂 Demo，包含：

- 宠物检测事件接入（支持 Frigate 事件 webhook）
- 自动投喂规则（时间窗、冷却、每日上限、连续命中）
- 手动投喂控制
- 手机浏览器远程控制页面
- 事件日志与状态持久化

## 功能点

1. 实时接收识别事件（猫/狗）
2. 满足规则后自动投喂一次
3. 冷却时间内防重复触发
4. 每日投喂次数限制
5. 指定时间窗生效
6. person 出现时可阻断投喂
7. 手机网页一键手动投喂
8. 自动模式开关
9. 最近事件列表与状态展示
10. 本地运行，后续可部署到 M2

## 目录结构

- app/: 后端服务
- web/: 手机控制页
- configs/demo_config.yaml: 配置文件
- scripts/start.sh: 一键启动脚本
- data/: 运行时自动生成（状态文件）

## 本机启动

```bash
cd /home/alister/workspace/pet-feeder-ai-demo
bash scripts/start.sh
```

启动后访问：

- 本机: http://127.0.0.1:8787
- 同网手机: http://<设备IP>:8787

## 新设备离线部署

已验证环境为 ARM64 Debian 13、GNOME 桌面及 Firefox。运行时独立安装到 `/opt/py312`，
不替换系统 Python；应用安装到 `/opt/pet-feeder-ai-demo`。新设备先校准系统时间，
否则每日配额和自动投喂时段会使用错误日期。

离线 Python 包可使用工作区的
`h1/debian/QuecPi-QCLinux-BL01/prebuild/python3.12.tar.gz`。
将包传到设备 `/tmp/pet-feeder-python312.tar.gz` 后，仅在 `/opt/py312` 尚不存在时执行：

```bash
mkdir -p /opt/py312
tar -xzf /tmp/pet-feeder-python312.tar.gz -C /opt/py312 --strip-components=2 ./usr
ln -s python3.12 /opt/py312/bin/python
/opt/py312/bin/python -m pip --version
```

部署应用的 `app`、`configs`、`models`、`scripts`、`systemd`、`web`、依赖文件和
`wheelhouse_aarch64`，不要复制旧设备的 `data`、虚拟环境和浏览器 profile。
当前相机配置使用稳定路径 `/dev/v4l/by-id/usb-icSpring_icspring_camera-video-index0`；
更换相机型号时应重新确认路径和实际采集能力。

在设备应用目录执行：

```bash
/opt/py312/bin/python -m pip install --no-index --find-links wheelhouse_aarch64 -r requirements-dev.txt
/opt/py312/bin/python -m pip check
/opt/py312/bin/python -m unittest discover -s scripts -p 'test_*.py' -v
chmod +x scripts/launch_device.sh
install -m 644 systemd/pet-feeder-ai-demo.service /etc/systemd/system/pet-feeder-ai-demo.service
install -m 644 systemd/pet-feeder-kiosk.service /etc/systemd/system/pet-feeder-kiosk.service
systemctl daemon-reload
systemctl enable --now pet-feeder-ai-demo.service pet-feeder-kiosk.service
```

当前项目配置为已验证的 M2 40Pin 第33脚舵机执行器。其他设备或接线未确认时，
先把 `feeder.backend` 改为 `mock`，完成空载核对后再启用硬件。
验收包括设备上的完整测试、持续变化的视频、上限设置重启后保留，以及真实前台窗口截图。

## M2 屏幕全屏运行

项目部署在 `/opt/pet-feeder-ai-demo`，后端服务为 `pet-feeder-ai-demo.service`。
设备需有已经登录的 GNOME 桌面、Firefox、curl 和 Xwayland。在设备项目目录以 root 执行：

```bash
chmod +x scripts/launch_device.sh
install -m 644 systemd/pet-feeder-kiosk.service /etc/systemd/system/pet-feeder-kiosk.service
systemctl daemon-reload
systemctl enable pet-feeder-kiosk.service
systemctl restart pet-feeder-kiosk.service
```

启动器先检查网页可达，再以前台进程运行浏览器。桌面或后端尚未就绪、浏览器异常退出时，
systemd 每 5 秒重新尝试。日志统一进入 journal；正常状态应为 `active (running)`，
不能用旧版 `oneshot` 的 `active (exited)` 判断浏览器是否存活。

每次启动会添加唯一的 `kiosk_start` 查询参数，避免继续使用旧首页的缓存或恢复状态。
浏览器使用专用 profile，不强杀其他 Firefox/Chromium，也不删除用户配置。
服务运行期间不要再单独执行启动器，以免同一个 profile 被重复打开。

```bash
systemctl show pet-feeder-kiosk.service -p MainPID -p SubState -p NRestarts
journalctl -u pet-feeder-kiosk.service -n 80 --no-pager
curl -fsS http://127.0.0.1:8787/api/live/status
/opt/py312/bin/python -m unittest discover -s scripts -p test_launch_device.py -v
```

验收需确认设备前台窗口确实显示左侧实时画面、右侧控制按钮，视频时间戳和画面持续变化。
仅检查首页 HTML、HTTP 200 或短暂存在的浏览器进程，不代表屏幕显示已通过验收。

### 实时预览性能

采集、画框和 JPEG 编码不等待 YOLO 推理或投喂事件处理。独立检测线程只读取最新一帧，
不排队积压旧画面；超过 1 秒的检测结果不再叠加，事件只按实际检测结果触发。
MJPEG 接口按帧序号发送新图，不重复发送旧图来增加表面帧率。

`live.fps` 是预览目标值，默认 30；相机仍可能受 USB 传输和自动曝光限制。
状态栏及 `/api/live/status` 分别显示 `preview_fps`、`inference_fps`、`inference_ms`，
不要把推理速度当作预览速度。当前 USB 相机使用 YUYV，V4L2 保留两个采集缓冲。
低照度下自动曝光可能降低原始帧率；缩短曝光虽然能提高帧率，也会使图像变暗，
本演示不自动覆盖用户的曝光设置。

```bash
python -m unittest discover -s scripts -p test_live_camera.py -v
```

## 关键 API

- GET /api/status
- GET /api/events
- POST /api/mode
- PUT /api/settings/feeding
- POST /api/records/reset
- POST /api/feed/manual
- POST /api/detections
- POST /api/detections/frigate

## 每日投喂上限

在右侧“每日投喂上限”输入 1–100 之间的整数，点击“保存设置”立即生效。
设置与今日成功投喂次数保存在 `data/state.json`，服务重启后保留。
修改上限不会清零已投喂次数；降低到当前次数以下会立即停止后续投喂。

自动与手动投喂共享同一额度和最少间隔。兼容保留 `bypass_cooldown` 请求字段，
但该字段不再允许绕过最少间隔，也不能跳过每日上限。
后端串行校验并执行投喂，并发请求也不能超额；失败的投喂不扣减额度。
计数沿用服务 UTC 日期，每日 UTC 00:00 重置；历史检测事件不会重置今日额度。

```bash
curl -X PUT http://127.0.0.1:8787/api/settings/feeding \
  -H 'Content-Type: application/json' \
  -d '{"daily_limit": 6}'
```

状态接口额外返回 `remaining_feeds`、`limit_reached` 和 `feeder_backend`。
限额、HTTP 接口、视频与启动回归测试：

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s scripts -p 'test_*.py' -v
```

界面使用本地 Lucide 0.468.0 图标，许可证见 `web/vendor/lucide.LICENSE`。

## 最少间隔与重置

“最少投喂间隔”单位为秒，范围 0–86400，默认 300 秒（5 分钟）。0 表示关闭间隔限制。
从最近一次成功投喂完成时开始计时，自动和手动投喂都必须等待；失败不重新开始计时。
冷却结束后，只要满足检测命中、时间窗、每日上限等条件，同一只一直留在画面里的动物
也可能再次触发。当前没有个体身份识别或“必须离开后才允许再次投喂”的规则。

次数和配置保存于 `data/state.json`。刷新网页或重新打开浏览器不会清零：

- “启动时重置记录”关闭：演示服务重启、设备重启后保留记录。
- 开启该选项并保存：本次不立即清零，下次演示后台服务启动时才重置；单独刷新网页不重置。
- 点击“重置记录”并确认：立即清除今日次数、最近投喂及冷却、人员阻断、命中和动态记录。

两种重置都会暂停自动投喂，避免清零后立即再次投喂；每日上限、最少间隔和启动重置选项保留。
需要继续自动演示时，重新打开“自动投喂”开关。默认不会替用户执行重置。

设置接口可部分更新，未提供的设置保持不变：

```bash
curl -X PUT http://127.0.0.1:8787/api/settings/feeding \
  -H 'Content-Type: application/json' \
  -d '{"cooldown_sec":300,"reset_on_start":false}'
```

立即重置需要显式确认：

```bash
curl -X POST http://127.0.0.1:8787/api/records/reset \
  -H 'Content-Type: application/json' \
  -d '{"confirmed":true}'
```

初始默认值可在 YAML 的 `rules.cooldown_sec`、`storage.reset_on_start` 配置。
界面已保存的设置优先，升级不覆盖它们；状态接口返回 `cooldown_sec`、`cooldown_left_sec`、
`reset_on_start` 和 `last_reset_at`。

## 对接 Frigate 说明

Frigate 事件可转发到：

- POST /api/detections/frigate

服务会从 payload.after 读取 label/score/camera/box 等字段并执行自动投喂规则。

## 舵机模拟投喂

`feeder.backend: servo` 会把每次允许的自动/手动投喂转换为一轮 50Hz PWM：
已校准的初始位置、下食位置、返回初始位置，并持续保持返回位置的 PWM。
后台启动时明确初始化已校准的原点，正常投喂之间仅改变占空比，不反复导出、禁用和释放通道。
正常退出和故障时才关闭输出、释放本进程申请的通道。构造执行器和打开网页不会另行驱动舵机。
`reference_confirmed` 默认 false：原点未校准时禁止PWM输出，暂停自动模式，界面显示未就绪。
最少间隔和每日上限继续生效；只有整轮软件执行成功才记为投喂。
没有位置或落料传感器，因此软件成功不代表已经确认机械动作或实际落料。

### 引脚核对

当前工作区 M2 模组 pinmap 和 Allegro 网表确认：

| 板端信号 | SoC/控制器 | 注意事项 |
| --- | --- | --- |
| J1501 40Pin 第33脚，PWM2_CH3_M2_3V3 | GPIO2_D3，pwm2_8ch_3，2ade3000.pwm | 当前 M2 源码已使用 pwm2m2_ch3 并启用；仍需核对设备实际加载的 DTB |
| PWM0_1V8，模组26脚 | GPIO0_C4，pwm0_2ch_0 | 已连接板载 LED，不作为默认舵机接口 |
| PWM1_1V8，模组190脚 | GPIO0_B5，pwm1_6ch_1 | 系统风扇占用，禁止挪用 |

`PMU_GPIO02` 尚未在这份 M2 pinmap 中找到，不能等同于 Linux 的 pwmchip2 或上述33脚。
若必须使用该信号，需要提供接口针脚图确认；本项目不自动修改设备树或抢占已有 PWM。
`pwmchip` 序号会变化，所以执行器按 sysfs `device` 的控制器名匹配，而不是固定序号。

### 接线与首次测试

1. 先确认舵机型号、位置式/连续旋转类型、供电和信号电平。使用符合舵机规格的独立电源，
  与开发板 GND 共地，不能从 GPIO 取电，也不能把舵机电源接入 PWM 信号脚。
2. 33脚在网表中为3.3V信号，接线前仍需与板卡版本核对；若舵机不接受该逻辑电平，使用合适的电平转换。
  首测先卸掉机械连杆或确保舵机不受阻，保留随时断开舵机外部电源的条件。
3. 设备连接后先执行只读检查，不会 export、enable 或改变输出：

```bash
cd /opt/pet-feeder-ai-demo
/opt/py312/bin/python scripts/inspect_servo_pwm.py --controller 2ade3000.pwm
```

检查器同时读取 `/sys/kernel/debug/pwm`（若权限允许）以查看内核消费者。
未在 sysfs 导出不代表没有内核消费者；导出遇到占用时执行器拒绝操作。
若控制器不存在，先核对板卡、运行中的设备树和 pinmux，不要猜别的 pwmchip 来代替。

4. 确认可用后先关闭自动投喂并校准原点，再配置以下参数。当前配置使用33脚，但原点确认默认关闭，
  不会把旧的1500微秒当作已经校准的机械原点。其他接线先切回 mock。

```yaml
feeder:
  backend: servo
  servo:
    controller: 2ade3000.pwm
    reference_confirmed: false
    channel: 0
    closed_pulse_us: 1500
    feed_pulse_us: 1700
    settle_ms: 1000
    hold_ms: 500
```

参数使用脉宽而非角度，1500/1700微秒只是初始配置，不是SG90机械端点的校准结果。
可配置脉宽限制为1000–2000微秒；50Hz周期为20毫秒。连续旋转舵机的含义不同，不能按位置角度使用。
SG90的1500微秒通常是中位，不是0度；手动掰轴不会记录当前位置，且可能损坏齿轮。
“180度舵机”描述可用行程，不表示每次下食必须扫满行程，更不能强顶机械限位。
先校准真正的关门脉宽和安全投喂脉宽，确认后再设置 `reference_confirmed: true`。
新控制在启动时只归到确认过的原点，投喂后保持该脉宽；软件读回仍不能替代物理位置反馈。

### SG90原点校准

原点未确认前禁止投喂；先停后台并断开机械连杆、确保行程无阻挡，不要靠手掰输出轴标零。
工具要求后台处于 inactive/failed，且必须显式授权动作。每次只发一个固定脉宽，最长5秒，
没有投喂或返回序列，不改计数；结束或中断后关闭PWM。不要将关闭后的失去保持力矩误认为“返回”。

```bash
systemctl stop pet-feeder-ai-demo.service
cd /opt/pet-feeder-ai-demo
/opt/py312/bin/python scripts/calibrate_servo_reference.py \
  --controller 2ade3000.pwm --pulse-us 1500 --seconds 3 --confirm-move
```

示例1500微秒用于观察预设中位，不是已经确认的原点。后续必须由操作者逐次确认，
小幅改变脉宽寻找需要的位置，禁止自动扫到机械硬限位。若保守1000–2000微秒范围不能满足机构，
先核对该SG90批次规格及舵盘安装，不擅自扩大脉宽或靠手顶限位。
确认位置后将相应数值保存为 `closed_pulse_us`，选择安全的 `feed_pulse_us`，最后开启
`reference_confirmed` 再启动后台。后台会先应用原点并持续保持；再手动验证一次往返后才开启自动。
`settle_ms` 是开环等待时间，不是到位传感器；返回仍不稳定时需继续核对实际脉冲、机械负载和舵机反馈。

5. 条件确认、供电接好后，手动点击一次投喂验证动作；冷却/上限不满足时不会运动，
  不要为了试转擅自清空用户计数。手动验证正常后，再启用自动投喂验证相同动作。

执行器不接管别人已导出的通道，本进程初始化后会保留自己的通道。动作或清理失败会锁定后续动作，并返回
`manual_check_required: true`；此时先断开舵机外部电源、检查机构和接线，不要连续点击重试。
确认故障排除并安全关闭/释放残留通道后，再由操作者重启后台解除进程内故障锁定。

首次导出的通道可能为 `period=0`，必须先写有效周期，再配置极性和脉宽。
内核不接受零周期状态下的 `duty_cycle` 或 `enable` 写入，即使值是0也可能返回 `EINVAL`。
若已有有效周期，先清零占空比再调整周期；清理时先读取使能状态，已关闭时直接释放自己申请的通道。
实际设备已验证33脚对应 `2ade3000.pwm`，本次枚举为 `pwmchip3`，运行DT复用为GPIO2_D3。
空载测试中脉宽与使能参数读回正确，输出已关闭释放；机械移动和返回仍需操作者目视确认，
不能仅根据软件成功响应认定舵机实际动作或已经落料。

```bash
python -m unittest discover -s scripts -p test_feeder.py -v
```

## 切换真实投喂命令

当前默认配置为舵机模式。需要改用其他外部投喂命令时，修改 configs/demo_config.yaml：

```yaml
feeder:
  backend: command
  command: "/usr/local/bin/feed_once.sh {reason}"
  command_timeout_sec: 5
```

## 手工测试自动投喂逻辑

发送三次高分猫检测（满足 required_hits=3）：

```bash
curl -X POST http://127.0.0.1:8787/api/detections \
  -H 'Content-Type: application/json' \
  -d '{"source":"manual","label":"cat","score":0.95,"bbox":[0.3,0.3,0.7,0.8]}'
```

重复发送 3 次可触发一次自动投喂。
