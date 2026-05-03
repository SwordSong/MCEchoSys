"""window.py

用于本地调试的桌面窗口：
- 左侧展示示例声骸卡片；
- 右侧展示详情和日志；
- 支持对 `src.detect_feature_code` 做热重载。

说明：项目在线运行入口是 `src.ui.overlay`，本文件更偏开发期实验/调试用途。
"""

import sys
import logging
import importlib
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QGridLayout, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QScrollArea,
    QFrame, QSplitter
)
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt
import threading
import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


# ===========================
# 声骸数据（示例）
# ===========================

SAMPLE_DATA = [
    {
        "name": "共鸣回响·芙露德莉斯",
        "cost": 4,
        "crit": "22%",
        "atk": 150,
        "hp": 390,
        "desc": "召唤破空幻刃攻击目标"
    },
    {
        "name": "晶化巨像",
        "cost": 3,
        "crit": "18%",
        "atk": 120,
        "hp": 420,
        "desc": "释放晶化冲击波"
    }
]


# ===========================
# 声骸卡片
# ===========================

class EchoCard(QPushButton):
    def __init__(self, data, click_callback):
        super().__init__(data["name"])
        self.data = data
        self.setFixedSize(120, 80)
        self.setStyleSheet("""
            QPushButton {
                background-color: #2c2f36;
                color: white;
                border: 1px solid #555;
                border-radius: 8px;
            }
            QPushButton:hover {
                background-color: #3c4048;
            }
        """)
        self.clicked.connect(lambda: click_callback(self.data))


# ===========================
# 主窗口
# ===========================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("鸣潮声骸管理界面")
        self.resize(1400, 800)

        # 主分割
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # ===========================
        # 左侧区域
        # ===========================

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)

        # 声骸滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)

        scroll_content = QWidget()
        self.grid = QGridLayout(scroll_content)

        # 添加卡片
        row = 0
        col = 0
        for data in SAMPLE_DATA:
            card = EchoCard(data, self.show_detail)
            self.grid.addWidget(card, row, col)
            col += 1
            if col > 3:
                col = 0
                row += 1

        scroll.setWidget(scroll_content)

        # 背景展示区（黑色区域）
        self.background_area = QLabel("背景图显示区域")
        self.background_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.background_area.setStyleSheet("""
            QLabel {
                background-color: black;
                color: white;
                font-size: 18px;
            }
        """)
        self.background_area.setFixedHeight(200)

        left_layout.addWidget(scroll)
        left_layout.addWidget(self.background_area)

        # ===========================
        # 右侧区域
        # ===========================

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        # 属性详情区
        self.detail_area = QFrame()
        self.detail_area.setStyleSheet("""
            QFrame {
                background-color: #1f2228;
                color: white;
                border-radius: 10px;
                padding: 10px;
            }
        """)

        self.detail_layout = QVBoxLayout(self.detail_area)

        self.detail_label = QLabel("请选择一个声骸")
        self.detail_label.setStyleSheet("font-size: 16px;")
        self.detail_layout.addWidget(self.detail_label)

        # 日志区域（红色区域）
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setStyleSheet("""
            QTextEdit {
                background-color: #8B0000;
                color: white;
                font-family: Consolas;
            }
        """)
        self.log_area.setFixedHeight(250)

        # 日志处理器（挂到 root，供插件复用）
        self.log_handler = LogHandler(log_area=self.log_area)
        self.log_handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        self.log_handler.setFormatter(formatter)
        root_logger = logging.getLogger()
        if self.log_handler not in root_logger.handlers:
            root_logger.addHandler(self.log_handler)
        root_logger.setLevel(logging.INFO)

        # 热重载按钮
        self.reload_button = QPushButton("热重载模块")
        self.reload_button.clicked.connect(self.reload_module)

        right_layout.addWidget(self.detail_area)
        right_layout.addWidget(self.log_area)
        right_layout.addWidget(self.reload_button)

        main_splitter.addWidget(left_widget)
        main_splitter.addWidget(right_widget)
        main_splitter.setSizes([700, 700])

        self.setCentralWidget(main_splitter)

        # 启动自动重载线程，并首次加载调试插件
        self.start_auto_reload("detect_feature_code")
        self.load_plugin("detect_feature_code")

    def load_plugin(self, module_name: str):
        """导入或重载插件模块，并挂接日志处理器。"""
        try:
            full_name = f"src.{module_name}"
            if full_name in sys.modules:
                module = importlib.reload(sys.modules[full_name])
            else:
                module = importlib.import_module(full_name)

            # 挂载日志处理器到插件 logger
            plugin_logger = logging.getLogger(full_name)
            if self.log_handler not in plugin_logger.handlers:
                plugin_logger.addHandler(self.log_handler)
            plugin_logger.setLevel(logging.INFO)
            plugin_logger.propagate = False

            # 调用插件的 register（可选）
            if hasattr(module, "register"):
                module.register({"log": self.log_area.append})

            self.log_area.append(f"[INFO] 模块 {full_name} 已加载")
        except Exception as e:
            self.log_area.append(f"[ERROR] 模块 {module_name} 加载失败: {e}")

    def start_auto_reload(self, module_name):
        """启动 watchdog 线程，监控指定模块文件变更后自动 reload。"""
        def watch_module():
            module_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", f"{module_name}.py"))
            event_handler = FileChangeHandler(module_name, module_path, self.load_plugin, self.log_area)
            observer = Observer()
            observer.schedule(event_handler, path=os.path.dirname(module_path), recursive=False)
            observer.start()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                observer.stop()
            observer.join()

        threading.Thread(target=watch_module, daemon=True).start()

    def reload_module(self):
        """手动热重载指定模块"""
        self.load_plugin("detect_feature_code")

    # ===========================
    # 点击卡片显示详情
    # ===========================

    def show_detail(self, data):
        text = f"""
名称: {data['name']}
COST: {data['cost']}
暴击: {data['crit']}
攻击: {data['atk']}
生命: {data['hp']}
技能: {data['desc']}
        """
        self.detail_label.setText(text)

        # 日志输出
        self.log_area.append(f"[INFO] 选择声骸: {data['name']}\n")

        # 示例背景图加载（如果有图片可替换）
        # pixmap = QPixmap("background.jpg")
        # self.background_area.setPixmap(pixmap.scaled(
        #     self.background_area.size(),
        #     Qt.AspectRatioMode.KeepAspectRatio
        # ))


# ===========================
# 日志处理器
# ===========================

class LogHandler(logging.Handler):
    def __init__(self, log_area):
        super().__init__()
        self.log_area = log_area

    def emit(self, record):
        log_entry = self.format(record)
        self.log_area.append(log_entry)




# ===========================
# 文件变化处理器
# ===========================

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, module_name, module_path, reload_cb, log_area):
        self.module_name = module_name
        self.module_path = os.path.abspath(module_path)
        self.reload_cb = reload_cb
        self.log_area = log_area

    def on_modified(self, event):
        if os.path.abspath(event.src_path) == self.module_path:
            try:
                self.reload_cb(self.module_name)
                self.log_area.append(f"[INFO] 模块 {self.module_name} 已自动重载！")
            except Exception as e:
                self.log_area.append(f"[ERROR] 模块 {self.module_name} 重载失败: {e}")


# ===========================
# 动态插件加载
# ===========================

def load_plugins(plugin_dir: str, app_ctx: dict):
    """
    动态加载插件模块并调用其 register 函数。
    :param plugin_dir: 插件目录路径
    :param app_ctx: 应用上下文
    """
    logger.info(f"Loading plugins from {plugin_dir}...")
    for filename in os.listdir(plugin_dir):
        if filename.endswith(".py") and filename != "__init__.py":
            module_name = f"src.{filename[:-3]}"  # 去掉 .py 后缀
            try:
                module = importlib.import_module(module_name)
                if hasattr(module, "register"):
                    module.register(app_ctx)
                    logger.info(f"Plugin {module_name} loaded successfully.")
                else:
                    logger.warning(f"Module {module_name} does not have a register function.")
            except Exception as e:
                logger.error(f"Failed to load plugin {module_name}: {e}")

# ===========================
# 启动
# ===========================


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())