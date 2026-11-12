"""自建合成应用(调优集, macOS, Qt 工具包): 与 qt_workbench 同一窗口, 停在表单页签(分组框/数字框/下拉/复选/单选/多行输入)。"""
import os
import runpy
import sys

os.environ["QT_SAMPLE_TAB"] = "1"
sys.argv = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "qt_workbench.py")]
runpy.run_path(sys.argv[0], run_name="__main__")
