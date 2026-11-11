"""第 8 轮交付首屏改写: data: 外置、退化 srcset 收回(纯文本变换, 不需浏览器)。"""
import base64

from core.clone.support import collapse_degenerate_srcset, externalize_data_uris


def _ws(tmp_path):
    (tmp_path / "static" / "img").mkdir(parents=True)
    (tmp_path / "static" / "img" / "a.jpg").write_bytes(b"x")
    return tmp_path


def test_srcset_all_same_file_falls_back_to_src(tmp_path):
    ws = _ws(tmp_path)
    html = ('<img src="/static/img/a.jpg" srcset="/static/img/b.jpg 10656w, /static/img/b.jpg 150w"'
            ' sizes="auto, 668px" alt="">')
    out, n = collapse_degenerate_srcset(html, ws)
    assert n == 1 and "srcset" not in out and "sizes" not in out and 'src="/static/img/a.jpg"' in out


def test_srcset_real_choices_or_script_kept(tmp_path):
    ws = _ws(tmp_path)
    keep = [
        '<img src="/static/img/a.jpg" srcset="/static/img/b.jpg 1x, /static/img/c.jpg 2x">',   # 真有选择
        '<img src="/static/img/zz.jpg" srcset="/static/img/b.jpg 1w, /static/img/b.jpg 2w">',  # src 不在盘上
        '<img src="https://x.test/a.jpg" srcset="/static/img/b.jpg 1w, /static/img/b.jpg 2w">',
        '<script>var t=\'<img src="/static/img/a.jpg" srcset="/static/img/b.jpg 1w, /static/img/b.jpg 2w">\'</script>',
    ]
    for h in keep:
        assert collapse_degenerate_srcset(h, ws) == (h, 0)


def test_externalize_big_data_uri(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 64
    uri = "data:image/png;base64," + base64.b64encode(raw).decode()
    out, n, nbytes = externalize_data_uris(f'<img src="{uri}"><script>var u="{uri}"</script>', tmp_path)
    assert n == 1 and nbytes == len(raw)
    assert out.startswith('<img src="/static/img/') and uri in out   # 脚本里的原样保留
    name = out.split('"')[1].lstrip("/")
    assert (tmp_path / name).read_bytes() == raw
