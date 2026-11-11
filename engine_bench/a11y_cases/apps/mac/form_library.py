"""自建合成应用(调优集, macOS): 资料库式窗口 —— 分段控件、搜索框、多列表格(表头 + 选中行)、标签页、多行文本、底部状态行。
需要 pyobjc-framework-Cocoa; 由 capture_ci.py macos 启动。"""
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered, NSBezelStyleRounded,
                    NSButton, NSFont, NSIndexSet, NSScrollView, NSSearchField, NSSegmentedControl, NSTableColumn,
                    NSTableView, NSTabView, NSTabViewItem, NSTextField, NSTextView, NSView, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSMakeRect, NSObject

ROWS = [("Field notes", "Text", "12 KB", "Today"), ("Budget draft", "Sheet", "48 KB", "Yesterday"),
        ("Site photos", "Folder", "--", "Mon"), ("Interview 03", "Audio", "3.1 MB", "Mon"),
        ("Outline v2", "Text", "6 KB", "Last week"), ("Reading list", "Text", "2 KB", "Last week"),
        ("Map export", "Image", "880 KB", "Oct 28"), ("Old drafts", "Folder", "--", "Oct 12")]
COLS = [("name", "Name", 220), ("kind", "Kind", 90), ("size", "Size", 90), ("date", "Modified", 110)]


class Source(NSObject):
    def numberOfRowsInTableView_(self, tv):
        return len(ROWS)

    def tableView_objectValueForTableColumn_row_(self, tv, col, row):
        return ROWS[row][[c[0] for c in COLS].index(str(col.identifier()))]


app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(40, 120, 860, 540),
    NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable,
    NSBackingStoreBuffered, False)
win.setTitle_("Library - Sample App")
v = win.contentView()

seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(20, 496, 240, 26))
seg.setSegmentCount_(3)
for i, t in enumerate(("All", "Recent", "Shared")):
    seg.setLabel_forSegment_(t, i)
    seg.setWidth_forSegment_(74, i)
seg.setSelectedSegment_(0)
v.addSubview_(seg)
search = NSSearchField.alloc().initWithFrame_(NSMakeRect(620, 496, 220, 24))
search.setPlaceholderString_("Search library")
v.addSubview_(search)
add = NSButton.alloc().initWithFrame_(NSMakeRect(270, 493, 100, 30))
add.setTitle_("New item")
add.setBezelStyle_(NSBezelStyleRounded)
v.addSubview_(add)

scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 250, 820, 232))
scroll.setHasVerticalScroller_(True)
scroll.setBorderType_(2)
table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 200))
for ident, title, width in COLS:
    col = NSTableColumn.alloc().initWithIdentifier_(ident)
    col.headerCell().setStringValue_(title)
    col.setWidth_(width)
    table.addTableColumn_(col)
src = Source.alloc().init()
table.setDataSource_(src)
table.setUsesAlternatingRowBackgroundColors_(True)
scroll.setDocumentView_(table)
v.addSubview_(scroll)
table.reloadData()
table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(1), False)

tabs = NSTabView.alloc().initWithFrame_(NSMakeRect(14, 44, 832, 196))
for title, body in (("Summary", "Budget draft\nSheet, 48 KB, modified yesterday.\n\nThree tabs: Plan, Actuals, Notes.\nShared with two people."),
                    ("History", "No changes this week."), ("Sharing", "Not shared.")):
    item = NSTabViewItem.alloc().initWithIdentifier_(title)
    item.setLabel_(title)
    pane = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 800, 150))
    tsv = NSScrollView.alloc().initWithFrame_(NSMakeRect(10, 10, 780, 130))
    tsv.setBorderType_(1)
    tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 760, 130))
    tv.setString_(body)
    tv.setFont_(NSFont.systemFontOfSize_(13))
    tsv.setDocumentView_(tv)
    pane.addSubview_(tsv)
    item.setView_(pane)
    tabs.addTabViewItem_(item)
v.addSubview_(tabs)

status = NSTextField.labelWithString_("8 items, 1 selected")
status.setFrame_(NSMakeRect(20, 14, 300, 18))
status.setFont_(NSFont.systemFontOfSize_(11))
v.addSubview_(status)
done = NSButton.alloc().initWithFrame_(NSMakeRect(740, 6, 100, 30))
done.setTitle_("Open")
done.setBezelStyle_(NSBezelStyleRounded)
done.setKeyEquivalent_("\r")
v.addSubview_(done)
win.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
