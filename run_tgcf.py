#!/usr/bin/env python3
"""tgcf启动脚本"""
import sys
import os

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入并运行tgcf
from tgcf.cli import app

if __name__ == "__main__":
    app()