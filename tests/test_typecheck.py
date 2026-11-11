"""类型检查基线守卫: 装了 mypy 时, 仓库根目录 `mypy`(读 mypy.ini)须 0 错误。

mypy 曾在引擎里查出两处真缺陷: app.py 手动 AI 修复入口漏导入 cloner_engine/ai_vision_repair(恒 NameError),
models.py 两个同名 close() 让后一个覆盖前一个。基线宽松起步, 防这类错误再进来。"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _has_mypy() -> bool:
    try:
        import mypy  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_mypy(), "未安装 mypy")
class TypeCheckBaseline(unittest.TestCase):
    def test_mypy_clean(self):
        proc = subprocess.run([sys.executable, "-m", "mypy"], cwd=ROOT, capture_output=True,
                              text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout[-4000:] + proc.stderr[-2000:])


class ManualAiRepairImports(unittest.TestCase):
    def test_lazy_imports_present(self):
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        body = src[src.index("def _run_ai_repair_once("):]
        body = body[:body.index("\ndef ", 10)]
        self.assertIn("from core import ai_vision_repair, cloner_engine", body)


if __name__ == "__main__":
    unittest.main()
