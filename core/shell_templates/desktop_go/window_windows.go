//go:build windows

package main

import (
	"syscall"
	"unsafe"

	"github.com/jchv/go-webview2"
)

// Windows: 系统 WebView2(Win10/11 自带运行时), 纯 Go 无 cgo, 可在 macOS/Linux 上交叉编译。
func runWindow(url string) {
	w := webview2.NewWithOptions(webview2.WebViewOptions{
		AutoFocus: true,
		WindowOptions: webview2.WindowOptions{Title: AppTitle, Width: uint(AppWidth), Height: uint(AppHeight), Center: true},
	})
	if w == nil {
		msg, _ := syscall.UTF16PtrFromString("未检测到 Microsoft Edge WebView2 运行时, 请先安装后再打开本应用。")
		title, _ := syscall.UTF16PtrFromString(AppTitle)
		syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(msg)), uintptr(unsafe.Pointer(title)), 0x10)
		return
	}
	defer w.Destroy()
	w.Navigate(url)
	w.Run()
}
