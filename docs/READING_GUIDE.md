# 鸣潮强化助手 (MC_SYS) 源码阅读指南

本文档由 GitHub Copilot 生成，旨在帮助开发者快速理清项目结构与核心逻辑。

## 1. 项目概览

本项目是一个基于“截图 -> OCR -> 规则判断”的桌面端辅助工具。
**核心价值**：帮助玩家在《鸣潮》游戏中自动识别声骸属性，并根据策略给出“强化”或“丢弃”的建议。

**技术栈**：
- **UI**: PyQt6 (实现透明置顶浮窗)
- **OCR**: PaddleOCR / RapidOCR (本地离线文字识别)
- **Game Detect**: pywin32 (窗口捕获)
- **Logic**: 纯 Python 规则引擎

---

## 2. 核心链路 (Pipeline)

数据流向如下：
`截屏(Source) -> 预处理(Preprocess) -> 文字识别(OCR) -> 语义解析(Parser) -> 概率/策略(Logic) -> 数据库(DB) -> UI展示`


### 关键文件阅读顺序

建议按照以下顺序阅读源码，最容易理解：

1.  **入口：[src/ui/overlay.py](src/ui/overlay.py)**
    *   **角色**：这是程序的 `main` 入口。
    *   **由于**：Python 的 GIL 限制，这里采取了 **多进程** 架构。
    *   **看点**：
        *   `OverlayWindow`: 绘制透明窗体。
        *   `_pipeline_process_entry`: 子进程函数，这里启动了 `PipelineRunner`。
        *   `mp.Queue`: 父子进程通信的桥梁。

2.  **主控：[src/pipeline.py](src/pipeline.py)**
    *   **角色**：后台核心调度器（由子进程运行）。
    *   **看点**：
        *   `PipelineRunner.tick()`: 这是整个系统的心跳函数，每秒运行约 1~5 次。
        *   **状态机**：`waiting_game_window` -> `waiting_uid` -> `running`。
        *   它是组装工：分别调用 Capture, Detector, OCR, Parser 模块。

3.  **捕获：[src/capture.py](src/capture.py)**
    *   **角色**：负责从 Windows 窗口获取图像。
    *   **看点**：
        *   `WindowCapture`: 封装了 `PrintWindow` (BitBlt) 和 `WindowsGraphicsCapture` (Win10+ 高性能录屏) 两种后端。

4.  **识别：[src/ocr.py](src/ocr.py)**
    *   **角色**：调用 PaddleOCR 提取文字。
    *   **看点**：
        *   `OCR` 类单例模式。
        *   `_build_ocr_with_compat`: 这一段复杂的逻辑是为了解决 PaddleOCR 在不同硬件上初始化崩溃的问题。

5.  **语义：[src/parser.py](src/parser.py) & [src/observation_extractor.py](src/observation_extractor.py)**
    *   **角色**：把 OCR 识别的一堆乱糟糟的文字（如 "暴 击 伤 害 7 . 5%"）转化成结构化数据（如 `{'name': '暴击伤害', 'value': 7.5}`）。
    *   **看点**：`extract_echo_observation` 函数，它是业务逻辑最密集的地方。

6.  **配置：[src/strategy_config.py](src/strategy_config.py)**
    *   **角色**：读取用户定义的策略（json文件）。

7.  **推算分析：[docs/辅音概率推算与保底机制.md](辅音概率推算与保底机制.md)**
    *   **角色**：说明如何用本地强化数据寻找辅音规律、成本止损策略和疑似保底机制。
    *   **看点**：连续无效、核心词条缺失、客户端重启、游戏日刷新等统计假设。

---

## 3. 重要调试工具

在开发过程中，你可能不需要每次都跑主程序。

-   **[tools/capture_and_crop.py](tools/capture_and_crop.py)**:
    -   手动截图工具。当你发现识别不准时，用这个脚本把游戏画面截下来，保存成图片，方便单独测试 OCR。

-   **[tools/debug_list_windows.py](tools/debug_list_windows.py)**:
    -   列出当前所有窗口句柄。如果程序找不到游戏窗口，用这个看看游戏窗口名叫什么。

---

## 4. 常见问题 (FAQ)

-   **为什么需要多进程？**
    OCR 运算非常消耗 CPU/GPU，如果在主线程跑，悬浮窗都会卡顿无法拖动。

-   **OCR 为什么有时候不准？**
    游戏字体的抗锯齿、背景透明度都会干扰。请查看 `src/preprocess.py` 中的图像增强算法。

-   **如何新增一种声骸属性？**
    修改 `data/echo_dictionary.json` (如果存在) 或直接修改 `src/parser.py` 中的映射表。
