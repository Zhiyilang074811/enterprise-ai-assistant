"""Initialize the platform database and default runtime files."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.database import init_db


if __name__ == "__main__":
    init_db()
    print("数据库初始化完成")
