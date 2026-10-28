#!/usr/bin/env python3
"""影视仓接口助手 —— PySide6 桌面版入口。

    python main.py            # 正常启动（真实网络）
    python main.py --demo     # 离线演示模式
"""
import sys

from ysc_helper.app import main

if __name__ == "__main__":
    sys.exit(main())
