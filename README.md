# 鸣潮强化识别工具

实时声骸面板识别、词条解析、调谐槽位判断和强化策略辅助工具。项目面向 Windows 桌面环境，使用本地截图、RapidOCR、YOLO 和 SQLite 完成离线识别与记录。

> 当前检查结果：`uv run python -B -m pytest tests/test_basic.py -q` 通过，56 个测试全部成功。

## 功能概览

| 模块 | 当前实现 |
| --- | --- |
| 窗口捕获 | 通过 `Client-Win64-Shipping.exe` 定位游戏窗口，支持 WGC / DXGI / PrintWindow 等后端回退 |
| 场景检测 | 使用 YOLOv8 定位 `echo_panel`、`enhance_panel` 面板 |
| OCR | 使用 RapidOCR + ONNX Runtime DirectML，本地离线识别中文文本 |
| 词条解析 | 标准化词条名，纠正常见 OCR 错字，输出 `name/value/is_pct` |
| 声骸观测 | 从 OCR 行文本提取声骸名、套装、主词条、COST、等级、槽位状态和副词条 |
| 策略建议 | 基于历史开孔事件、词条优先级、边际强化成本和下一孔概率给出继续、暂存、抽离或换声骸建议 |
| 数据存储 | SQLite + SQLAlchemy，记录账号、登录、声骸信息和辅音开孔事件 |
| 浮窗 UI | PyQt6 透明置顶窗口，使用结构化 `echo` 对象渲染主面板、辅音槽和强化建议 |
| 标注训练 | 提供采样、标注、YOLO 微调脚本 |

## 环境要求

- Windows 10/11
- Python 3.11
- uv
- 鸣潮客户端进程名默认匹配 `Client-Win64-Shipping.exe`
- 完整 YOLO 检测需要安装 `gpu` extra，并准备模型文件 `models/yolov8_custom.pt`

## 快速开始

```powershell
# 安装 uv（如未安装）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 同步基础依赖
uv sync

# 同步 YOLO / PyTorch / ultralytics 依赖
uv sync --extra gpu

# 同步测试和打包依赖
uv sync --extra dev

# 运行测试
uv run pytest

# 启动浮窗（需先打开游戏）
uv run python -m src.ui.overlay
```

也可以直接使用启动脚本：

```powershell
scripts\run_gpu.bat
```

说明：

- OCR 当前不再使用 PaddleOCR；README 中旧的 Paddle / `ocr-cpu` 安装步骤已移除。
- `uv sync --extra gpu` 会安装 PyTorch、torchvision、torchaudio 和 ultralytics，用于 YOLO 检测和训练。
- 默认 YOLO 模型路径是 `models/yolov8_custom.pt`，可通过 `MC_YOLO_MODEL_PATH` 覆盖。

## 常用命令

```powershell
# 浮窗 + 后台管线
uv run python -m src.ui.overlay

# 控制台管线
uv run python -m src.pipeline

# 截图 / 捕获诊断
uv run python -m src.capture

# UID 特征码检测
uv run python -m src.detect_feature_code

# 词条解析自测
uv run python -m src.parser

# 云端同步 worker
uv run python -m src.sync --url https://your-api/upload --token YOUR_TOKEN

# 标注工具
uv run python -m tools.annotator --images outputs/yolo_train --labels data/labels --classes echo_panel,enhance_panel

# YOLO 微调
uv run python -m tools.finetune_yolo --images outputs/yolo_train --labels data/labels --dataset data/yolo_dataset --classes echo_panel,enhance_panel --epochs 80 --imgsz 640 --base yolov8n.pt

# 打包 Windows 版本
scripts\build_windows.bat
```

## 运行时开关

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MC_YOLO_MODEL_PATH` | `models/yolov8_custom.pt` | 指定 YOLO 模型路径 |
| `MC_DETECTOR_CONF` | `0.35` | YOLO 检测置信度 |
| `MC_UID_CROP_BOX` | `0.98,0.88,1.0,0.98` | UID OCR 裁剪比例，格式为 `x1,y1,x2,y2` |
| `MC_UID_LOCK_CONFIRMATIONS` | `2` | UID 连续一致多少次后锁定账号 |
| `MC_DISABLE_UID` | `0` | 跳过 UID 守卫，适合先联调面板识别 |
| `MC_DISABLE_OCR` | `0` | 禁用 OCR，同时会禁用 UID 和面板 OCR |
| `MC_DISABLE_PANEL_OCR` | `0` | 只禁用面板 OCR |
| `MC_DISABLE_DB` | `0` | 禁用数据库写入 |
| `MC_DISABLE_STRATEGY` | `0` | 禁用策略建议计算 |
| `MC_CAPTURE_ONLY` | `0` | 只运行捕获链路 |
| `MC_DEBUG_DUMP` | `0` | 保存 UID / 面板调试截图到程序运行目录的 `logs/recognition` |
| `MC_IDLE_INTERVAL` | `0.3` | 浮窗子进程空闲轮询间隔 |
| `MC_ACTIVE_INTERVAL` | `1.5` | 浮窗子进程识别成功后的轮询间隔 |

示例：

```powershell
# UID 裁剪偏移时，临时放宽右下角裁剪框
$env:MC_UID_CROP_BOX="0.95,0.82,1.0,0.995"
uv run python -m src.ui.overlay

# 只联调 YOLO / OCR 面板识别
$env:MC_DISABLE_UID="1"
uv run python -m src.ui.overlay
```

## 项目结构

```text
mc_sys/
├── src/
│   ├── capture.py              # Windows 窗口捕获和 CaptureWorker
│   ├── detector.py             # YOLOv8 推理封装
│   ├── detect_feature_code.py  # UID 特征码识别
│   ├── ocr.py                  # RapidOCR + DirectML 封装
│   ├── preprocess.py           # 裁剪、缩放、OCR 图像增强
│   ├── parser.py               # 词条解析与常见 OCR 纠错
│   ├── observation_extractor.py# 声骸观测提取、槽位状态判断
│   ├── probability.py          # 频率模型、贝叶斯更新、动作建议
│   ├── strategy_config.py      # 策略优先级配置加载
│   ├── pipeline.py             # 端到端调度管线
│   ├── db.py                   # SQLAlchemy 模型和数据库初始化
│   ├── sync.py                 # REST 同步 worker
│   └── ui/
│       ├── overlay.py          # PyQt6 浮窗入口，多进程管线
│       └── window.py           # 旧窗口/独立 UI 组件
├── tools/
│   ├── capture_yolo_samples.py # YOLO 训练样本采集
│   ├── annotator.py            # bbox 标注工具
│   ├── finetune_yolo.py        # YOLOv8 微调
│   └── pick_rgb.py             # 取色和比例坐标调试
├── data/
│   ├── panel_layout.json       # 场景类别、面板比例区域
│   ├── echo_dictionary.json    # 声骸、套装、主词条词典
│   ├── substat_values.json     # 副词条离散数值和档位
│   └── strategy_priority.json  # 套装/词条优先级权重
├── models/
│   └── yolov8_custom.pt        # 默认 YOLO 检测模型
├── tests/
│   └── test_basic.py
├── docs/
│   ├── architecture.md
│   ├── READING_GUIDE.md
│   └── 辅音概率推算与保底机制.md
├── scripts/
│   ├── run_gpu.bat
│   └── build_windows.bat
├── pyproject.toml
└── uv.lock
```

## 核心链路

```text
Overlay UI
  -> pipeline 子进程
  -> CaptureWorker 获取游戏帧
  -> 记录游戏 PID / 启动时间快照
  -> UID OCR 锁定账号
  -> YOLO 判断面板场景
  -> RapidOCR 识别面板文本
  -> parser / observation_extractor 结构化声骸信息
  -> 生成 echo 对象返回 UI
  -> probability / strategy_config 生成建议
  -> SQLite 入库
  -> Queue 回传 UI
```

`PipelineRunner` 对 OCR、YOLO 和数据库连接使用懒加载：创建对象时不会立刻加载模型，只有进入实际识别路径时才初始化重资源。游戏进程的 PID 与启动时间会被记录为同一个快照，并写入账号和登录记录，用于判断客户端是否重启。

## 数据库结构

默认数据库为用户数据目录下的 `mc_enhance.db`，Windows 路径通常是 `%LOCALAPPDATA%\mc-enhance-helper\mc_enhance.db`。旧版本里的 `echo_sessions` 已改为 `echo_info`，`enhance_events` 已改为 `echo_substats`，`enhance_actions`、`samples` 和 `substat_definitions` 已删除。辅音档位配置以用户数据目录中的 `data/substat_values.json` 为准。

数据库中的时间字段写入本机本地时间，不使用 UTC。

### `accounts` 账号表

记录本机识别到的游戏账号和强化计数。

| 字段 | 含义 |
| --- | --- |
| `id` | 本机自增账号 ID，不等于游戏 UID。 |
| `uid` | 游戏右下角特征码/UID，账号唯一标识。 |
| `name` | 当前电脑名称，用于区分本机来源。 |
| `created_at` | 账号记录创建时间。 |
| `account_hash` | 由 `id + uid + name + created_at` 生成的 16 位账号 hash。 |
| `total_enhance` | 该账号累计强化/开孔次数，不包含历史补录。 |
| `today_enhance` | 今日强化次数，按凌晨 4 点切换游戏日。 |
| `client_enhance` | 当前游戏客户端进程内强化次数，客户端 PID 变化后归零。 |
| `last_client_start_at` | 最近一次检测到的游戏客户端启动时间。 |
| `last_client_pid` | 最近一次检测到的游戏客户端 PID。 |

### `echo_info` 声骸信息表

记录已确认实例 ID 的声骸。空白辅音声骸不会生成实例 ID；出现第一个辅音后，才按声骸身份和已有辅音生成 `echo_instance_id`。

| 字段 | 含义 |
| --- | --- |
| `account_id` | 归属账号 ID，与 `echo_instance_id` 组成联合主键。 |
| `echo_instance_id` | 声骸实例 ID，由声骸名、套装、主属性、已有辅音顺序生成。 |
| `uid` | 游戏 UID 冗余字段，方便导出和分析。 |
| `echo_name` | 声骸名称。 |
| `cost` | 声骸 COST，只允许 `1 / 3 / 4`。 |
| `set_name` | 套装名称。 |
| `main_stat` | 主词条。 |
| `initial_substat_count` | 首次生成实例 ID 时已有的辅音数量，范围 `1~5`。 |
| `created_at` | 记录创建时间。 |

### `echo_substats` 声骸辅音表

记录每个孔位开出的辅音。一次多开会写入多条记录，并共享同一个 `action_id`。

| 字段 | 含义 |
| --- | --- |
| `id` | 本地自增辅音记录 ID，用于后续上传数据库和按断点续传。 |
| `event_id` | 单条辅音记录 UUID。 |
| `session_id` | 关联 `echo_info.echo_instance_id`；字段名沿用旧版 session 命名。 |
| `action_id` | 同一次开孔动作的分组 ID。 |
| `account_id` | 归属账号 ID，冗余保存便于查询。 |
| `action_type` | 动作类型：`single` 单开、`multi` 多开、`unknown` 未知、`history` 历史补录。 |
| `action_open_count` | 本次动作新增孔数。 |
| `action_start_level` | 本次动作开始对应的开孔等级。 |
| `action_end_level` | 本次动作结束对应的开孔等级。 |
| `action_span_holes` | 本次动作涉及孔位，例如 `2,3`。 |
| `slot_index` | 当前辅音所在孔位，范围 `1~5`。 |
| `level_before` | 开该孔前对应等级，通常为 `5 / 10 / 15 / 20 / 25`。 |
| `substat_name` | 辅音名称。 |
| `substat_value` | 辅音数值。 |
| `value_tier` | 数值档位，范围 `1~4`。 |
| `is_historical_unknown` | 是否为启动后首次看到已有辅音时补录的历史未知记录。 |
| `game_day_index` | 游戏日序号，用于凌晨 4 点刷新今日统计。 |
| `is_first_enhance_of_day` | 是否当天第一次强化。 |
| `is_just_logged_in` | 是否刚锁定 UID/登录后的第一次强化。 |
| `is_just_client_restarted` | 是否客户端重启后的第一次强化。 |
| `restart_open_index` | 客户端重启后第几次开孔。 |
| `day_enhance_count` | 截止当前事件的当日强化计数。 |
| `source_region` | OCR 来源区域标识。 |
| `ocr_confidence` | OCR 置信度。 |
| `created_at` | 记录创建时间。 |

### `login_records` 登录记录表

记录 UID 锁定时看到的客户端进程信息，用于判断是否重启游戏客户端。

| 字段 | 含义 |
| --- | --- |
| `login_id` | 登录记录 UUID。 |
| `account_id` | 归属账号 ID。 |
| `login_at` | 本次锁定 UID 的时间。 |
| `client_started_at` | 检测到的游戏客户端启动时间。 |
| `client_pid` | 检测到的游戏客户端 PID。 |
| `is_client_restart` | 本次是否判定为客户端重启。 |
| `created_at` | 记录创建时间。 |

## 配置文件

| 文件 | 说明 |
| --- | --- |
| `data/panel_layout.json` | YOLO 类别、检测置信度、面板比例区域 |
| `data/echo_dictionary.json` | 用于匹配声骸名、套装名、主词条 |
| `data/substat_values.json` | 副词条允许值、档位、百分比类型 |
| `data/strategy_priority.json` | 默认与套装专属词条优先级 |

这些配置支持运行时热重载，默认每 2 秒检查一次文件变更。

运行时会优先读取用户数据目录下的 `data/*.json`，Windows 路径通常是 `%LOCALAPPDATA%\mc-enhance-helper\data\`。首次启动时程序会从仓库/打包资源复制默认 JSON；后续更新 exe 不会覆盖用户已经修改过的配置。

## 标注与训练

采集样本：

```powershell
uv run python -m tools.capture_yolo_samples --scene echo_panel --output outputs/yolo_train/echo_panel --count 40 --interval 1.0
uv run python -m tools.capture_yolo_samples --scene enhance_panel --output outputs/yolo_train/enhance_panel --count 40 --interval 1.0
```

标注样本：

```powershell
uv run python -m tools.annotator --images outputs/yolo_train --labels data/labels --classes echo_panel,enhance_panel
```

训练模型：

```powershell
uv run python -m tools.finetune_yolo --images outputs/yolo_train --labels data/labels --dataset data/yolo_dataset --classes echo_panel,enhance_panel --epochs 80 --imgsz 640 --base yolov8n.pt
```

训练完成后，默认输出模型为 `models/yolov8_custom.pt`。

## 开发检查

```powershell
# 单元测试
uv run pytest

# 语法编译检查
uv run python -m compileall src

# 查看当前变更
git status --short
```

本次检查中，测试结果为：

```text
56 passed
```

## 常见问题

**启动后没有识别结果**

确认游戏进程正在运行，模型文件存在于 `models/yolov8_custom.pt`，并且已经执行 `uv sync --extra gpu`。

**UID 识别位置偏了**

使用 `MC_UID_CROP_BOX` 调整右下角裁剪范围，例如：

```powershell
$env:MC_UID_CROP_BOX="0.95,0.82,1.0,0.995"
```

**浮窗卡顿**

浮窗和识别管线已经分进程运行。若仍然卡顿，可调大 `MC_ACTIVE_INTERVAL`，或先用 `MC_CAPTURE_ONLY=1` 排查捕获链路。

**OCR 后端是什么**

当前代码使用 RapidOCR + `onnxruntime-directml`。`rapidocr-onnxruntime` 只能用 `--no-deps` 安装，否则会把 CPU 版 `onnxruntime` 拉回开发环境；打包脚本已按这个方式处理。历史文档中提到的 PaddleOCR 已不是当前主路径。
