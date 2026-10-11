"""ブラウザを使わない URL 探索 (m3u8dl/scan.py) と GUI のフォールバック経路の検証。

ブラウザを起動できない環境でも最低限の結果を返せるようにするための機能なので、
「ページの HTML に書かれた URL」「参照している JavaScript に書かれた URL (相対・エスケープ付き)」
「取得に失敗したときの記録」を実測で押さえる。GUI 側は実際の mainloop を回して、
探索で見つかったプレイリストが一覧に反映されるところまで確認する。
"""

from __future__ import annotations

import functools
import http.server
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl import gui  # noqa: E402
from m3u8dl.scan import _extract_playlists, scan_page_for_playlists  # noqa: E402

# 探索対象の URL が一覧に現れるまでの待ち時間 (秒)
_GUI_WAIT_SECONDS = 25.0

# テスト用ページ: インラインの URL と、参照している JavaScript を 1 つ持つ
_PAGE_HTML = """<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>テストページ</title></head>
<body>
<script>
  // 絶対 URL (クエリ付き) と、JS のエスケープ表記
  window.config = { src: 'https://cdn.example.com/live/master.m3u8?token=abc' };
  var escaped = "https:\\/\\/cdn.example.com\\/vod\\/index.m3u8";
  var notPlaylist = 'https://cdn.example.com/thumb.mp4';
</script>
<script src="/app.js"></script>
</body>
</html>
"""

# 相対パスで書かれたプレイリスト (ページの URL を基準に解決されることを見る)
_APP_JS = """const player = createPlayer({
  src: 'hls/playlist.m3u8?session=1',
  fallback: './hls/backup.m3u8',
});
"""


class _QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """ログ出力を抑制した静的ファイル配信ハンドラー。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 親のシグネチャに合わせる
        """アクセスログを捨てる。"""
        return


class PlaylistExtractionTest(unittest.TestCase):
    """テキストからのプレイリスト URL 抽出の検証。"""

    def test_extracts_absolute_relative_and_escaped_urls(self) -> None:
        """絶対 URL・相対 URL・JS のエスケープ表記を解決できること。"""
        text = "a('https://cdn.example.com/x.m3u8?q=1') b('hls/y.m3u8') c('https:\\/\\/cdn.example.com\\/z.m3u8')"
        found = _extract_playlists(text, 'https://site.example.com/page/index.html')
        self.assertIn('https://cdn.example.com/x.m3u8?q=1', found)
        self.assertIn('https://site.example.com/page/hls/y.m3u8', found)
        self.assertIn('https://cdn.example.com/z.m3u8', found)

    def test_ignores_non_playlist_paths_and_duplicates(self) -> None:
        """プレイリスト以外の拡張子を拾わず、同じ URL を重複させないこと。"""
        text = "a('https://cdn.example.com/video.mp4') b('hls/a.m3u8') c('hls/a.m3u8')"
        found = _extract_playlists(text, 'https://site.example.com/p.html')
        self.assertEqual(found, ['https://site.example.com/hls/a.m3u8'])


class ScanPageTest(unittest.TestCase):
    """ページと JavaScript を実際に取得する検証。"""

    tmp_root: Path
    httpd: http.server.ThreadingHTTPServer
    http_thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """テスト用のページを配信するローカル HTTP サーバーを起動する。"""
        cls.tmp_root = Path(tempfile.mkdtemp(prefix='m3u8dl_scan_'))
        (cls.tmp_root / 'page.html').write_text(_PAGE_HTML, encoding='utf-8')
        (cls.tmp_root / 'app.js').write_text(_APP_JS, encoding='utf-8')

        handler = functools.partial(_QuietHTTPRequestHandler, directory=str(cls.tmp_root))
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base_url = f'http://127.0.0.1:{cls.httpd.server_address[1]}'

    @classmethod
    def tearDownClass(cls) -> None:
        """HTTP サーバーを停止して一時ディレクトリを片付ける。"""
        if getattr(cls, 'httpd', None) is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.http_thread.join(timeout=5.0)
        if getattr(cls, 'tmp_root', None) is not None:
            import shutil

            shutil.rmtree(cls.tmp_root, ignore_errors=True)

    def test_scan_finds_playlists_in_html_and_referenced_js(self) -> None:
        """HTML と参照先 JavaScript の両方からプレイリスト URL を見つけること。"""
        result = scan_page_for_playlists(f'{self.base_url}/page.html', timeout=10.0)

        self.assertIn('https://cdn.example.com/live/master.m3u8?token=abc', result.playlists)
        self.assertIn('https://cdn.example.com/vod/index.m3u8', result.playlists)
        self.assertIn(f'{self.base_url}/hls/playlist.m3u8?session=1', result.playlists)
        self.assertIn(f'{self.base_url}/hls/backup.m3u8', result.playlists)
        # プレイリスト以外は拾わない
        self.assertNotIn('https://cdn.example.com/thumb.mp4', result.playlists)
        # ページ本体と、参照されていた JavaScript の両方を取得している
        self.assertIn(f'{self.base_url}/page.html', result.fetched_urls)
        self.assertIn(f'{self.base_url}/app.js', result.fetched_urls)

    def test_scan_records_error_for_missing_page(self) -> None:
        """存在しないページはエラーとして記録され、例外にならないこと。"""
        result = scan_page_for_playlists(f'{self.base_url}/missing.html', timeout=10.0)
        self.assertEqual(result.playlists, [])
        self.assertTrue(result.errors, msg='取得失敗が記録されていない')

    def test_scan_rejects_non_http_scheme(self) -> None:
        """http / https 以外の URL は ValueError になること。"""
        with self.assertRaises(ValueError):
            scan_page_for_playlists('file:///C:/tmp/page.html')


class GuiScanFallbackTest(ScanPageTest):
    """GUI の「URL を抽出」経路が実 mainloop で動くことの検証。"""

    def test_scan_button_registers_playlists(self) -> None:
        """「URL を抽出」で見つかったプレイリストが一覧へ追加されること。"""
        root = tk.Tk()
        root.withdraw()
        app = gui.DownloaderApp(root)

        state: dict[str, object] = {'found': [], 'done': False}
        deadline = time.monotonic() + _GUI_WAIT_SECONDS

        def poll() -> None:
            """JavaScript 由来の URL が一覧に現れるまで待つ (現れたら mainloop を抜ける)。"""
            found = [url for url in app.tree_items if '.m3u8' in url]
            # HTML 由来の URL は先に届くため、参照先 JavaScript の URL が来るまで待つ
            ready = any('playlist.m3u8' in url for url in found)
            if ready or time.monotonic() > deadline:
                state['found'] = found
                state['done'] = True
                root.quit()
            else:
                root.after(200, poll)

        try:
            app.url_var.set(f'{self.base_url}/page.html')
            app._on_scan_page()
            root.after(200, poll)
            root.mainloop()
        finally:
            root.destroy()

        self.assertTrue(state['done'], msg='探索が終わらなかった')
        found = state['found']
        self.assertTrue(found, msg='一覧にプレイリストが追加されていない')
        self.assertTrue(
            any('playlist.m3u8' in url for url in found),
            msg=f'JavaScript に書かれた URL が一覧に無い: {found}',
        )


if __name__ == '__main__':
    unittest.main()
