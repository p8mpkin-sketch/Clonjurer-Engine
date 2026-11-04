// Clonjurer 应用壳(桌面, 系统 WebView): 本地服务器语义与导出的 Flask 工程一致, 只监听 127.0.0.1。
// 页面路由表 → templates/; 录制接口(精确方法+路径 → 同路径 → undefined 段折叠 → 后缀段匹配);
// 影子文件直通; 开启反代兜底且有上游时, 未命中请求转发上游; 否则 404。
package main

import (
	"bytes"
	"embed"
	"encoding/json"
	"io"
	"io/fs"
	"mime"
	"net"
	"net/http"
	"path"
	"regexp"
	"strconv"
	"strings"
	"time"
)

//go:embed all:site
var siteFS embed.FS

type siteMeta struct {
	Pages    map[string]string `json:"pages"`
	Upstream string            `json:"upstream"`
	Proxy    bool              `json:"proxy"`
}

type mockEntry struct {
	Path         string      `json:"path"`
	Method       string      `json:"method"`
	Status       interface{} `json:"status"`
	ResponseBody string      `json:"response_body"`
}

type server struct {
	site  fs.FS
	meta  siteMeta
	mocks []mockEntry
}

var trimSlash = regexp.MustCompile(`^/+|/+$`)
var undefSeg = regexp.MustCompile(`(^|/)undefined(/|$)`)

func strip(p string) string { return trimSlash.ReplaceAllString(p, "") }
func norm(p string) string  { return "/" + strip(p) }

func segs(p string) []string {
	out := []string{}
	for _, s := range strings.Split(p, "/") {
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

func newServer() *server {
	sub, _ := fs.Sub(siteFS, "site")
	s := &server{site: sub}
	if b, err := fs.ReadFile(sub, "site_meta.json"); err == nil {
		_ = json.Unmarshal(b, &s.meta)
	}
	if s.meta.Pages == nil {
		s.meta.Pages = map[string]string{"/": "index.html"}
	}
	if b, err := fs.ReadFile(sub, "mock_data.json"); err == nil {
		_ = json.Unmarshal(b, &s.mocks)
	}
	return s
}

func methodOf(m mockEntry) string {
	if m.Method == "" {
		return "GET"
	}
	return strings.ToUpper(m.Method)
}

func (s *server) exact(t, method string) *mockEntry {
	var loose *mockEntry
	for i := range s.mocks {
		m := &s.mocks[i]
		if norm(m.Path) != t {
			continue
		}
		if methodOf(*m) == method {
			return m
		}
		if loose == nil {
			loose = m
		}
	}
	return loose
}

func (s *server) matchMock(p, method string) *mockEntry {
	method = strings.ToUpper(method)
	target := norm(p)
	hit := s.exact(target, method)
	if hit == nil && strings.Contains(target, "undefined/") {
		hit = s.exact(norm(undefSeg.ReplaceAllString(target, "/")), method)
	}
	if hit == nil {
		req := segs(target)
		var best *mockEntry
		bestLen := 0
		for i := range s.mocks {
			m := &s.mocks[i]
			sg := segs(norm(m.Path))
			if len(sg) >= bestLen && len(req) > len(sg) && strings.Join(req[len(req)-len(sg):], "/") == strings.Join(sg, "/") {
				if methodOf(*m) == method {
					return m
				}
				best, bestLen = m, len(sg)
			}
		}
		hit = best
	}
	return hit
}

func mimeOf(rel string) string {
	name := path.Base(rel)
	if i := strings.Index(name, "@"); i >= 0 {
		name = name[:i]
	}
	if t := mime.TypeByExtension(strings.ToLower(path.Ext(name))); t != "" {
		return t
	}
	switch strings.ToLower(path.Ext(name)) {
	case ".woff2":
		return "font/woff2"
	case ".woff":
		return "font/woff"
	case ".js", ".mjs":
		return "text/javascript; charset=utf-8"
	}
	return "application/octet-stream"
}

func (s *server) sendFile(w http.ResponseWriter, rel string) bool {
	if strings.Contains(rel, "..") {
		return false
	}
	b, err := fs.ReadFile(s.site, rel)
	if err != nil {
		return false
	}
	w.Header().Set("Content-Type", mimeOf(rel))
	w.Header().Set("Cache-Control", "no-cache")
	_, _ = w.Write(b)
	return true
}

var hopHeaders = map[string]bool{"Host": true, "Connection": true, "Accept-Encoding": true, "Content-Length": true,
	"Transfer-Encoding": true, "Origin": true, "Referer": true}

func (s *server) proxy(w http.ResponseWriter, r *http.Request, sub string) {
	body, _ := io.ReadAll(r.Body)
	u := strings.TrimRight(s.meta.Upstream, "/") + "/" + sub
	if r.URL.RawQuery != "" {
		u += "?" + r.URL.RawQuery
	}
	req, err := http.NewRequest(r.Method, u, bytes.NewReader(body))
	if err != nil {
		http.Error(w, "Upstream proxy failed", http.StatusBadGateway)
		return
	}
	for k, v := range r.Header {
		if !hopHeaders[http.CanonicalHeaderKey(k)] {
			req.Header[k] = v
		}
	}
	req.Header.Set("Referer", s.meta.Upstream+"/")
	req.Header.Set("Origin", s.meta.Upstream)
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		http.Error(w, "Upstream proxy failed", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); ct != "" {
		w.Header().Set("Content-Type", ct)
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	sub := strip(r.URL.Path)
	if sub == "" {
		page := s.meta.Pages["/"]
		if page == "" {
			page = "index.html"
		}
		if !s.sendFile(w, "templates/"+page) {
			http.NotFound(w, r)
		}
		return
	}
	for _, k := range []string{"/" + sub, "/" + sub + "/", sub} {
		if page, ok := s.meta.Pages[k]; ok && s.sendFile(w, "templates/"+page) {
			return
		}
	}
	if m := s.matchMock(sub, r.Method); m != nil {
		ctype := "text/plain; charset=utf-8"
		var probe interface{}
		if m.ResponseBody != "" && json.Unmarshal([]byte(m.ResponseBody), &probe) == nil {
			ctype = "application/json; charset=utf-8"
		}
		code := 200
		switch v := m.Status.(type) {
		case float64:
			code = int(v)
		case string:
			if n, err := strconv.Atoi(v); err == nil {
				code = n
			}
		}
		w.Header().Set("Content-Type", ctype)
		w.Header().Set("X-Mock-Api", "recorded")
		w.WriteHeader(code)
		_, _ = io.WriteString(w, m.ResponseBody)
		return
	}
	if s.sendFile(w, sub) {
		return
	}
	if !strings.Contains("/"+sub, "/undefined/") && s.meta.Proxy && s.meta.Upstream != "" {
		s.proxy(w, r, sub)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusNotFound)
	_, _ = io.WriteString(w, "<h1>404 - Not Cloned</h1>")
}

// start 在 127.0.0.1 上监听; 优先固定端口(页面 localStorage/Cookie 跨次启动保留), 被占用退回随机端口。
func start(port int) (string, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(port))
	if err != nil {
		ln, err = net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return "", err
		}
	}
	go func() { _ = http.Serve(ln, newServer()) }()
	return "http://" + ln.Addr().String() + "/", nil
}
