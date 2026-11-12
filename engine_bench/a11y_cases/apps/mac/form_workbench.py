"""自建合成应用(调优集, macOS): 工作台式窗口 —— 工具栏(图标 + 文字)、左右分栏、侧栏大纲(分组 + 展开的子项)、
带网格线的表格、分组框里的复选/单选/弹出菜单/滑块/进度条、底部路径行。
需要 pyobjc-framework-Cocoa; 由 capture_ci.py macos 启动。"""
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered, NSBox, NSButton,
                    NSButtonTypeRadio, NSButtonTypeSwitch, NSFont, NSImage, NSIndexSet, NSOutlineView,
                    NSPopUpButton, NSProgressIndicator, NSScrollView, NSSlider, NSSplitView, NSTableColumn,
                    NSTableView, NSTextField, NSToolbar, NSToolbarItem, NSView, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSMutableDictionary, NSObject

TOOLS = [("new", "New", "doc.badge.plus"), ("open", "Open", "folder"), ("save", "Save", "square.and.arrow.down"),
         ("run", "Run", "play.fill"), ("stop", "Stop", "stop.fill"), ("info", "Inspect", "info.circle")]
TREE = [("Projects", ["Harbor survey", "Orchard map", "Night trains"]),
        ("Collections", ["Favourites", "Drafts", "Archive 2019"]),
        ("Devices", ["Card reader", "Scanner"])]
ROWS = [("1", "station_a", "54.20", "open", "ok"), ("2", "station_b", "17.05", "closed", "ok"),
        ("3", "depot_north", "88.90", "open", "late"), ("4", "depot_south", "3.40", "open", "ok"),
        ("5", "junction_7", "41.00", "closed", "missing"), ("6", "siding_2", "9.75", "open", "ok"),
        ("7", "yard_east", "63.10", "open", "late")]
COLS = [("n", "#", 40), ("name", "Name", 170), ("load", "Load", 80), ("state", "State", 90), ("check", "Check", 90)]


def node(title, kids=()):
    d = NSMutableDictionary.dictionary()
    d["title"] = title
    d["kids"] = [node(k) for k in kids]
    return d


NODES = [node(t, k) for t, k in TREE]


class Outline(NSObject):
    def outlineView_numberOfChildrenOfItem_(self, ov, item):
        return len(NODES) if item is None else len(item["kids"])

    def outlineView_isItemExpandable_(self, ov, item):
        return len(item["kids"]) > 0

    def outlineView_child_ofItem_(self, ov, index, item):
        return NODES[index] if item is None else item["kids"][index]

    def outlineView_objectValueForTableColumn_byItem_(self, ov, col, item):
        return item["title"]


class Table(NSObject):
    def numberOfRowsInTableView_(self, tv):
        return len(ROWS)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):
        return ROWS[row][[c[0] for c in COLS].index(str(col.identifier()))]


class Bar(NSObject):
    def toolbarAllowedItemIdentifiers_(self, tb):
        return [t[0] for t in TOOLS]

    def toolbarDefaultItemIdentifiers_(self, tb):
        return [t[0] for t in TOOLS]

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, tb, ident, flag):
        item = NSToolbarItem.alloc().initWithItemIdentifier_(ident)
        for key, label, symbol in TOOLS:
            if key == str(ident):
                item.setLabel_(label)
                item.setPaletteLabel_(label)
                img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, label)
                if img is not None:
                    item.setImage_(img)
        return item


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(60, 100, 900, 560),
    NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable,
    NSBackingStoreBuffered, False)
win.setTitle_("Workbench - Sample App")
bar_delegate = Bar.alloc().init()
toolbar = NSToolbar.alloc().initWithIdentifier_("workbench")
toolbar.setDelegate_(bar_delegate)
toolbar.setDisplayMode_(1)      # 图标 + 文字
win.setToolbar_(toolbar)
v = win.contentView()

split = NSSplitView.alloc().initWithFrame_(NSMakeRect(0, 30, 900, 530))
split.setVertical_(True)
split.setDividerStyle_(2)

side = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, 220, 530))
side.setHasVerticalScroller_(True)
outline = NSOutlineView.alloc().initWithFrame_(NSMakeRect(0, 0, 220, 530))
ocol = NSTableColumn.alloc().initWithIdentifier_("title")
ocol.setWidth_(196)
ocol.headerCell().setStringValue_("Sources")
outline.addTableColumn_(ocol)
outline.setOutlineTableColumn_(ocol)
osrc = Outline.alloc().init()
outline.setDataSource_(osrc)
side.setDocumentView_(outline)
outline.reloadData()
outline.expandItem_(NODES[0])
outline.expandItem_(NODES[1])
outline.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(2), False)
split.addSubview_(side)

main = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 670, 530))
head = NSTextField.labelWithString_("Orchard map")
head.setFrame_(NSMakeRect(16, 492, 400, 24))
head.setFont_(NSFont.boldSystemFontOfSize_(17))
main.addSubview_(head)
sub = NSTextField.labelWithString_("7 records, last synced 09:41")
sub.setFrame_(NSMakeRect(16, 472, 400, 16))
sub.setFont_(NSFont.systemFontOfSize_(11))
main.addSubview_(sub)

scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, 236, 638, 226))
scroll.setHasVerticalScroller_(True)
scroll.setBorderType_(1)
table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 620, 200))
for ident, title, width in COLS:
    col = NSTableColumn.alloc().initWithIdentifier_(ident)
    col.headerCell().setStringValue_(title)
    col.setWidth_(width)
    table.addTableColumn_(col)
tsrc = Table.alloc().init()
table.setDataSource_(tsrc)
table.setGridStyleMask_(3)      # 横竖网格线
scroll.setDocumentView_(table)
main.addSubview_(scroll)
table.reloadData()
table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(2), False)

box = NSBox.alloc().initWithFrame_(NSMakeRect(14, 16, 642, 208))
box.setTitle_("Sync options")
inner = box.contentView()
chk1 = NSButton.alloc().initWithFrame_(NSMakeRect(14, 150, 280, 20))
chk1.setButtonType_(NSButtonTypeSwitch)
chk1.setTitle_("Sync automatically every hour")
chk1.setState_(1)
inner.addSubview_(chk1)
chk2 = NSButton.alloc().initWithFrame_(NSMakeRect(14, 124, 280, 20))
chk2.setButtonType_(NSButtonTypeSwitch)
chk2.setTitle_("Include closed records")
inner.addSubview_(chk2)
for i, t in enumerate(("Keep newest copy", "Keep both copies", "Ask every time")):
    rb = NSButton.alloc().initWithFrame_(NSMakeRect(330, 150 - i * 26, 260, 20))
    rb.setButtonType_(NSButtonTypeRadio)
    rb.setTitle_(t)
    rb.setState_(1 if i == 1 else 0)
    inner.addSubview_(rb)
lab = NSTextField.labelWithString_("Region:")
lab.setFrame_(NSMakeRect(14, 86, 70, 18))
inner.addSubview_(lab)
pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(86, 80, 190, 28), False)
pop.addItemsWithTitles_(["Northern line", "Coastal loop", "Inland freight"])
inner.addSubview_(pop)
lab2 = NSTextField.labelWithString_("Detail:")
lab2.setFrame_(NSMakeRect(14, 50, 70, 18))
inner.addSubview_(lab2)
sl = NSSlider.alloc().initWithFrame_(NSMakeRect(86, 46, 190, 24))
sl.setMinValue_(0)
sl.setMaxValue_(100)
sl.setDoubleValue_(65)
inner.addSubview_(sl)
lab3 = NSTextField.labelWithString_("Uploading 4 of 7")
lab3.setFrame_(NSMakeRect(330, 50, 200, 18))
inner.addSubview_(lab3)
prog = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(330, 26, 280, 16))
prog.setIndeterminate_(False)
prog.setMinValue_(0)
prog.setMaxValue_(7)
prog.setDoubleValue_(4)
inner.addSubview_(prog)
main.addSubview_(box)
split.addSubview_(main)
v.addSubview_(split)
split.setPosition_ofDividerAtIndex_(220, 0)

path = NSTextField.labelWithString_("Collections  >  Drafts  >  Orchard map")
path.setFrame_(NSMakeRect(12, 7, 600, 16))
path.setFont_(NSFont.systemFontOfSize_(11))
v.addSubview_(path)
win.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
