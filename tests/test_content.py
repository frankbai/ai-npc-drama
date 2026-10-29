"""内容包检查：结构 + 防剧透（不调用模型）。改了剧情先跑这个。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import check_content  # noqa: E402


class ContentTest(unittest.TestCase):
    def test_sample_story(self):
        root = ROOT / "content"
        self.assertEqual(check_content.validate(root), [])
        self.assertEqual(check_content.spoilers(root), [])

    def test_fixture_pack(self):
        root = ROOT / "tests" / "fixtures" / "pack"
        self.assertEqual(check_content.validate(root), [])
        self.assertEqual(check_content.spoilers(root), [])

    def test_checker_catches_problems(self):
        """故意弄坏一个内容包，检查工具应该能发现。"""
        import json
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / "pack"
        shutil.copytree(ROOT / "tests" / "fixtures" / "pack", tmp)
        ev = json.loads((tmp / "events.json").read_text(encoding="utf-8"))
        ev["events"][0]["gives"] = ["不存在的线索"]
        (tmp / "events.json").write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
        (tmp / "npcs" / "beta" / "secret.md").write_text("## 影子\n你就是凶手。\n", encoding="utf-8")
        errs = check_content.validate(tmp)
        self.assertTrue(any("不存在的线索" in e for e in errs))
        spoil = check_content.spoilers(tmp)
        self.assertTrue(any("你就是凶手" in e for e in spoil))


if __name__ == "__main__":
    unittest.main()
