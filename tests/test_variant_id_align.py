"""窄屏变体: 容器子项标签序列相同但带 id 的子项只在一侧存在 → 容器整体作为变体根(_id_misaligned_parents)。"""
import unittest

from core.cloner_engine import _id_misaligned_parents


def _row(tag, kids, ids):
    return [tag, "", "", kids, "", "", "", "", ids]


class IdMisalignedParents(unittest.TestCase):
    def test_component_only_on_one_side_lifts_parent(self):
        # 主页: [背景, A, 占位, 说明]; 窄屏页: [背景, 占位, B, 说明] —— 标签序列相同, A 只在主页、B 只在窄屏页
        main = {"div:1": _row("DIV", "DIV,DIV,DIV,DIV", ",compA,,note"),
                "div:1>div:2": _row("DIV", "", "")}
        other = {"div:1": _row("DIV", "DIV,DIV,DIV,DIV", ",,compB,note"),
                 "div:1>div:2": _row("DIV", "", "")}
        self.assertEqual(_id_misaligned_parents(main, other), ["div:1"])

    def test_same_ids_or_moved_ids_do_not_lift(self):
        main = {"div:1": _row("DIV", "DIV,DIV", "a,b"), "div:2": _row("DIV", "DIV", "c")}
        same = {"div:1": _row("DIV", "DIV,DIV", "a,b"), "div:2": _row("DIV", "DIV", "c")}
        self.assertEqual(_id_misaligned_parents(main, same), [])
        # 两个 id 只是换了位置(另一页里都存在): 不是"另一页没有这个组件"
        moved = {"div:1": _row("DIV", "DIV,DIV", "b,a"), "div:2": _row("DIV", "DIV", "c")}
        self.assertEqual(_id_misaligned_parents(main, moved), [])

    def test_tag_sequence_differs_is_left_to_structure_rule(self):
        main = {"div:1": _row("DIV", "DIV,DIV", "a,b")}
        other = {"div:1": _row("DIV", "DIV,SPAN", "a,x")}
        self.assertEqual(_id_misaligned_parents(main, other), [])

    def test_old_rows_without_id_field_are_ignored(self):
        main = {"div:1": ["DIV", "", "", "DIV,DIV", "", "", "", ""]}
        other = {"div:1": _row("DIV", "DIV,DIV", "a,b")}
        self.assertEqual(_id_misaligned_parents(main, other), [])


if __name__ == "__main__":
    unittest.main()
