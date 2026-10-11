"""コンソール出力が Windows の文字コード (cp932) で表せることの検証。

main.py の `print()` の出力は cp932 のコンソールやリダイレクト先へそのまま流れるため、
cp932 で表せない文字が混ざっていると UnicodeEncodeError で出力が止まる
(実際に「--diagnose が結果を残さずに終わる」不具合を起こした)。
レポートファイルは UTF-8 で書くため影響を受けないが、切り分けの取っ掛かりを失わないよう
コンソールへ出すソースは cp932 で表せる文字だけで書く。

GUI の文字列は Tk のウィジェット (内部は UTF-8) にしか出ないため対象外とする。
"""

from __future__ import annotations

import sys
import unicodedata
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


class ConsoleEncodingTest(unittest.TestCase):
    """コンソールへ出力するソースの文字種を確認する。"""

    def test_main_py_is_encodable_in_cp932(self) -> None:
        """main.py が cp932 で表せる文字だけで書かれていること。"""
        text = (REPOSITORY_ROOT / 'main.py').read_text(encoding='utf-8')
        unsupported: dict[str, int] = {}
        for character in text:
            try:
                character.encode('cp932')
            except UnicodeEncodeError:
                name = f'U+{ord(character):04X} {unicodedata.name(character, "UNKNOWN")}'
                unsupported[name] = unsupported.get(name, 0) + 1
        self.assertEqual(unsupported, {}, f'cp932 で表せない文字がある: {unsupported}')


if __name__ == '__main__':
    unittest.main()
