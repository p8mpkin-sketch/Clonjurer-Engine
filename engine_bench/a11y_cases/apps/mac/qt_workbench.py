"""自建合成应用(调优集, macOS, Qt 工具包): 跨平台工具包自绘控件的窗口 —— 工具栏(图标下文字)、分栏、树、
页签、带表头的表格、表单(下拉框/数字框/复选/单选/单行与多行输入)、状态栏。
原生 AppKit 之外的工具包暴露的无障碍树形状不同(角色名、分组层级、框的口径), 调优集需要覆盖这一类。
需要 PySide6; 由 capture_ci.py macos 启动。"""
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QRadioButton, QSpinBox,
                               QSplitter, QStyle, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

ROWS = [("A-104", "Lantern, brass", "12", "Shelf 3", "In stock"), ("A-221", "Rope, 20 m", "4", "Shelf 1", "Low"),
        ("B-017", "Canvas roll", "9", "Floor", "In stock"), ("B-130", "Tin cups", "48", "Shelf 2", "In stock"),
        ("C-002", "Signal flags", "0", "--", "Ordered"), ("C-045", "Oil can", "7", "Shelf 3", "In stock"),
        ("D-311", "Spare glass", "2", "Crate 5", "Low"), ("D-312", "Wicks, pack", "30", "Crate 5", "In stock")]

app = QApplication(sys.argv)
win = QMainWindow()
win.setWindowTitle("Stockroom - Sample App")
win.resize(920, 580)
win.setUnifiedTitleAndToolBarOnMac(False)

tb = win.addToolBar("Main")
tb.setMovable(False)
tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
st = app.style()
for label, icon in (("New", QStyle.SP_FileIcon), ("Open", QStyle.SP_DirOpenIcon), ("Save", QStyle.SP_DialogSaveButton),
                    ("Refresh", QStyle.SP_BrowserReload), ("Remove", QStyle.SP_TrashIcon)):
    tb.addAction(QAction(st.standardIcon(icon), label, win))
tb.addSeparator()
tb.addAction(QAction(st.standardIcon(QStyle.SP_MessageBoxInformation), "About", win))

split = QSplitter(Qt.Horizontal)
tree = QTreeWidget()
tree.setHeaderLabels(["Location", "Items"])
for name, kids in (("Warehouse", (("Shelf 1", "14"), ("Shelf 2", "52"), ("Shelf 3", "19"), ("Floor", "9"))),
                   ("Loading bay", (("Crate 5", "32"), ("Crate 6", "0"))), ("On order", ())):
    top = QTreeWidgetItem(tree, [name, str(sum(int(k[1]) for k in kids))])
    for k in kids:
        QTreeWidgetItem(top, list(k))
tree.expandAll()
tree.setColumnWidth(0, 150)
tree.setCurrentItem(tree.topLevelItem(0).child(2))
split.addWidget(tree)

tabs = QTabWidget()
page = QWidget()
lay = QVBoxLayout(page)
row = QHBoxLayout()
row.addWidget(QLabel("Filter:"))
flt = QLineEdit()
flt.setPlaceholderText("Type a code or a name")
row.addWidget(flt)
kind = QComboBox()
kind.addItems(["All states", "In stock", "Low", "Ordered"])
row.addWidget(kind)
row.addWidget(QPushButton("Apply"))
lay.addLayout(row)
table = QTableWidget(len(ROWS), 5)
table.setHorizontalHeaderLabels(["Code", "Description", "Qty", "Place", "State"])
for r, vals in enumerate(ROWS):
    for c, val in enumerate(vals):
        table.setItem(r, c, QTableWidgetItem(val))
table.setColumnWidth(1, 200)
table.selectRow(3)
lay.addWidget(table)
tabs.addTab(page, "Browse")

form_page = QWidget()
fl = QVBoxLayout(form_page)
grp = QGroupBox("Item details")
f = QFormLayout(grp)
code = QLineEdit("B-130")
f.addRow("Code:", code)
f.addRow("Description:", QLineEdit("Tin cups"))
qty = QSpinBox()
qty.setRange(0, 999)
qty.setValue(48)
f.addRow("Quantity:", qty)
place = QComboBox()
place.addItems(["Shelf 2", "Shelf 1", "Shelf 3", "Floor"])
f.addRow("Place:", place)
fl.addWidget(grp)
opts = QGroupBox("Reorder")
ol = QVBoxLayout(opts)
c1 = QCheckBox("Reorder when quantity falls below 10")
c1.setChecked(True)
ol.addWidget(c1)
ol.addWidget(QCheckBox("Notify the stockroom lead"))
r1 = QRadioButton("Usual supplier")
r1.setChecked(True)
ol.addWidget(r1)
ol.addWidget(QRadioButton("Ask for three quotes"))
fl.addWidget(opts)
notes = QPlainTextEdit("Counted on Tuesday.\nTwo cups dented, kept for spares.")
fl.addWidget(notes)
tabs.addTab(form_page, "Edit")
tabs.addTab(QWidget(), "History")
tabs.setCurrentIndex(int(os.environ.get("QT_SAMPLE_TAB", "0")))
split.addWidget(tabs)
split.setSizes([240, 680])
win.setCentralWidget(split)
win.statusBar().showMessage("8 items listed, 2 low, 1 on order")
win.show()
win.raise_()
sys.exit(app.exec())
