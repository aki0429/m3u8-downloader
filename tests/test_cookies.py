"""Cookie 処理 (m3u8dl.cookies) の実測テスト。

各種入力形式の解析、Cookie ヘッダ生成、CDP 結果の変換、Netscape 形式の
保存と読み込みのラウンドトリップを検証する。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.cookies import (  # noqa: E402
    CookieEntry,
    from_cdp_cookies,
    load_file,
    merge_entries,
    parse_cookie_text,
    save_netscape,
    to_cookie_header,
)


class TestParseCookieText(unittest.TestCase):
    """parse_cookie_text の各入力形式を検証する。"""

    def test_simple_pairs(self) -> None:
        """'a=1; b=2' 形式を解析できること。"""
        entries = parse_cookie_text('a=1; b=2;  c = 3 ')
        self.assertEqual(
            [(entry.name, entry.value) for entry in entries],
            [('a', '1'), ('b', '2'), ('c', '3')],
        )

    def test_cookie_prefix(self) -> None:
        """'Cookie: a=1; b=2' のプレフィックスを除去して解析すること。"""
        entries = parse_cookie_text('Cookie: session=abc; token=xyz')
        self.assertEqual(
            [(entry.name, entry.value) for entry in entries],
            [('session', 'abc'), ('token', 'xyz')],
        )

    def test_newline_separated(self) -> None:
        """改行区切りの key=value も解析できること。"""
        entries = parse_cookie_text('a=1\nb=2\n')
        self.assertEqual([entry.name for entry in entries], ['a', 'b'])

    def test_netscape_format(self) -> None:
        """Netscape cookies.txt 形式を解析できること。"""
        text = (
            '# Netscape HTTP Cookie File\n'
            '.example.com\tTRUE\t/\tFALSE\t0\tsession\tabc123\n'
            '#HttpOnly_.example.com\tTRUE\t/secure\tTRUE\t1893456000\ttoken\tdeadbeef\n'
        )
        entries = parse_cookie_text(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, 'session')
        self.assertEqual(entries[0].value, 'abc123')
        self.assertEqual(entries[0].domain, '.example.com')
        self.assertEqual(entries[0].path, '/')
        # HttpOnly 行は接頭辞を外してドメインを取り出す
        self.assertEqual(entries[1].name, 'token')
        self.assertEqual(entries[1].domain, '.example.com')
        self.assertEqual(entries[1].path, '/secure')

    def test_json_array(self) -> None:
        """Chrome 拡張風の JSON 配列を解析できること。"""
        text = '[{"name": "a", "value": "1", "domain": ".x.com", "path": "/"}, {"name": "b", "value": "2"}]'
        entries = parse_cookie_text(text)
        self.assertEqual([entry.name for entry in entries], ['a', 'b'])
        self.assertEqual(entries[0].domain, '.x.com')
        self.assertEqual(entries[1].path, '/')

    def test_cdp_wrapped_json(self) -> None:
        """{'cookies': [...]} 形式の JSON を解析できること。"""
        text = '{"cookies": [{"name": "sid", "value": "s1", "domain": "y.com", "path": "/"}]}'
        entries = parse_cookie_text(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, 'sid')

    def test_empty_input(self) -> None:
        """空文字では空リストを返すこと。"""
        self.assertEqual(parse_cookie_text(''), [])
        self.assertEqual(parse_cookie_text('   '), [])


class TestToCookieHeader(unittest.TestCase):
    """to_cookie_header を検証する。"""

    def test_join(self) -> None:
        """CookieEntry 群を 'a=1; b=2' 形式に連結すること。"""
        entries = [CookieEntry('a', '1'), CookieEntry('b', '2')]
        self.assertEqual(to_cookie_header(entries), 'a=1; b=2')

    def test_domain_filter(self) -> None:
        """URL 指定時はドメインに一致する Cookie だけを出力すること。"""
        entries = [
            CookieEntry('global', 'g'),
            CookieEntry('site', 's', domain='.example.com'),
            CookieEntry('other', 'o', domain='.other.com'),
        ]
        header = to_cookie_header(entries, 'https://sub.example.com/watch')
        self.assertIn('global=g', header)
        self.assertIn('site=s', header)
        self.assertNotIn('other=o', header)

    def test_path_filter(self) -> None:
        """URL 指定時はパスに一致する Cookie だけを出力すること。"""
        entries = [
            CookieEntry('root', 'r', domain='.example.com', path='/'),
            CookieEntry('secure', 'x', domain='.example.com', path='/secure'),
        ]
        header = to_cookie_header(entries, 'https://example.com/secure/page')
        self.assertIn('root=r', header)
        self.assertIn('secure=x', header)
        header2 = to_cookie_header(entries, 'https://example.com/public')
        self.assertIn('root=r', header2)
        self.assertNotIn('secure=x', header2)


class TestMergeEntries(unittest.TestCase):
    """merge_entries を検証する。"""

    def test_later_wins(self) -> None:
        """同名 Cookie は後勝ちになること。"""
        first = [CookieEntry('a', '1'), CookieEntry('b', '2')]
        second = [CookieEntry('a', '9'), CookieEntry('c', '3')]
        merged = merge_entries(first, second)
        result = {entry.name: entry.value for entry in merged}
        self.assertEqual(result['a'], '9')
        self.assertEqual(result['b'], '2')
        self.assertEqual(result['c'], '3')
        # 出現順は最初の位置が保たれること
        self.assertEqual([entry.name for entry in merged], ['a', 'b', 'c'])


class TestNetscapeRoundTrip(unittest.TestCase):
    """save_netscape と load_file のラウンドトリップを検証する。"""

    def test_save_and_load(self) -> None:
        """保存した Cookie をそのまま読み戻せること。"""
        entries = [
            CookieEntry('session', 'abc123', domain='.example.com', path='/'),
            CookieEntry('token', 'xyz', domain='example.com', path='/api'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cookies.txt'
            save_netscape(entries, path)
            self.assertTrue(path.exists())
            loaded = load_file(path)

        self.assertEqual(
            [(entry.name, entry.value, entry.domain, entry.path) for entry in loaded],
            [(entry.name, entry.value, entry.domain, entry.path) for entry in entries],
        )

    def test_load_json_file(self) -> None:
        """拡張子 .json のファイルを読み込めること。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cookies.json'
            path.write_text('[{"name": "n", "value": "v", "domain": "z.com", "path": "/"}]', encoding='utf-8')
            loaded = load_file(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].name, 'n')
        self.assertEqual(loaded[0].domain, 'z.com')


class TestFromCdpCookies(unittest.TestCase):
    """from_cdp_cookies を検証する。"""

    def test_basic_conversion(self) -> None:
        """CDP 形式の辞書を CookieEntry に変換すること。"""
        cookies = [
            {'name': 'a', 'value': '1', 'domain': '.x.com', 'path': '/'},
            {'name': 'b', 'value': None, 'domain': 'x.com', 'path': '/p'},
            {'name': '', 'value': 'ignored'},
        ]
        entries = from_cdp_cookies(cookies)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, 'a')
        self.assertEqual(entries[1].value, '')


if __name__ == '__main__':
    unittest.main()
