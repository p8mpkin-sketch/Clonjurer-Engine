"""自建合成应用(调优集, macOS): 偏好设置式表单窗口 —— 标签、输入框、勾选/单选、下拉、滑块、按钮。
需要 pyobjc-framework-Cocoa; 由 capture_ci.py macos 启动。"""
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered, NSBezelStyleRounded,
                    NSButton, NSButtonTypeRadio, NSButtonTypeSwitch, NSFont, NSPopUpButton, NSSlider, NSTextField, NSWindow,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskTitled)
from Foundation import NSMakeRect

app = NSApplication.sharedApplication()
app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    NSMakeRect(60, 160, 640, 420),
    NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable, NSBackingStoreBuffered, False)
win.setTitle_("Preferences - Sample App")
v = win.contentView()


def label(text, x, y, w=150, size=13, bold=False):
    t = NSTextField.labelWithString_(text)
    t.setFrame_(NSMakeRect(x, y, w, 22 if size <= 14 else 30))
    t.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    v.addSubview_(t)


def field(text, x, y, w=300):
    t = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, 24))
    t.setStringValue_(text)
    v.addSubview_(t)


def button(title, x, y, w=110, kind=None, on=False):
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 30 if kind is None else 22))
    b.setTitle_(title)
    if kind is None:
        b.setBezelStyle_(NSBezelStyleRounded)
    else:
        b.setButtonType_(kind)
        b.setState_(1 if on else 0)
    v.addSubview_(b)
    return b


label("General", 30, 370, 300, size=20, bold=True)
label("Choose how the sample workspace behaves.", 30, 342, 420)
label("Display name", 30, 300)
field("Alex Example", 190, 298)
label("Workspace", 30, 262)
field("north-shelf", 190, 260)
label("Start page", 30, 224)
pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(188, 220, 200, 26), False)
for it in ("Overview", "Recent files", "Inbox"):
    pop.addItemWithTitle_(it)
v.addSubview_(pop)
button("Open the last project on launch", 190, 186, 320, NSButtonTypeSwitch, True)
button("Send anonymous usage statistics", 190, 160, 320, NSButtonTypeSwitch, False)
label("Theme", 30, 124)
button("Light", 190, 124, 80, NSButtonTypeRadio, True)
button("Dark", 280, 124, 80, NSButtonTypeRadio, False)
label("Text size", 30, 86)
sl = NSSlider.alloc().initWithFrame_(NSMakeRect(190, 84, 300, 24))
sl.setMinValue_(0)
sl.setMaxValue_(100)
sl.setDoubleValue_(40)
v.addSubview_(sl)
button("Cancel", 390, 24)
ok = button("Save", 510, 24)
ok.setKeyEquivalent_("\r")
win.makeKeyAndOrderFront_(None)
app.activateIgnoringOtherApps_(True)
app.run()
