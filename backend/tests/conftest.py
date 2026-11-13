"""让测试无论从哪里运行都能 import 到 backend 包。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
