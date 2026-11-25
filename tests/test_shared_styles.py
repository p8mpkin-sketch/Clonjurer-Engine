"""重复大 <style> 外置共享(影子根构造样式表逐根物化导致 HTML 爆炸, Top1000 ibm.com 153MB)。"""
import tempfile
import unittest
from pathlib import Path

from core.clone.support import externalize_shared_styles

BIG = ".a{color:red}" * 300


class SharedStyles(unittest.TestCase):
    def test_duplicates_become_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            html = ('<div><template shadowrootmode="open"><style data-clone-adopted>' + BIG + '</style><p>1</p></template></div>') * 5
            out, n, saved = externalize_shared_styles(html, Path(d))
            self.assertEqual(n, 1)
            self.assertEqual(out.count('<link rel="stylesheet" href="/static/css/'), 5)
            self.assertNotIn("<style", out)
            self.assertIn("data-clone-adopted", out)
            files = list((Path(d) / "static" / "css").glob("*.css"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), BIG)
            self.assertGreater(saved, 0)

    def test_unique_small_relative_and_script_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            rel = ".b{background:url(img/x.png)}" * 200
            html = ("<style>" + BIG + "</style>"                        # 只出现一次
                    + ("<style>.s{}</style>" * 3)                      # 太小
                    + ("<style>" + rel + "</style>") * 2              # 相对 url() 外置后基准会变
                    + "<script>var s='<style>" + BIG + "</style><style>" + BIG + "</style>'</script>")
            out, n, _ = externalize_shared_styles(html, Path(d))
            self.assertEqual(n, 0)
            self.assertEqual(out, html)


if __name__ == "__main__":
    unittest.main()
