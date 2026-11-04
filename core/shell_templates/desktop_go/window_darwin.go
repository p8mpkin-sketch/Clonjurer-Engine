//go:build darwin

package main

import webview "github.com/webview/webview_go"

// macOS: 系统 WKWebView(cgo), 不内置浏览器内核。
func runWindow(url string) {
	w := webview.New(false)
	defer w.Destroy()
	w.SetTitle(AppTitle)
	w.SetSize(AppWidth, AppHeight, webview.HintNone)
	w.Navigate(url)
	w.Run()
}
