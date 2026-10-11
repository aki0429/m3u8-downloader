"""CDP ブラウザ解析モジュール (browser.py / websocket_client.py) の実測検証テスト。

外部サイトには一切依存せず、127.0.0.1 で起動した一時 HTTP サーバーだけを使う。
ローカルの `index.html` が `fetch('./test.m3u8')` と `fetch('./segment0.ts')` を実行し、
`BrowserSniffer` がブラウザを実際に起動してそれらを検出できることを確認する。
"""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.browser import BrowserSniffer, _classify  # noqa: E402
from m3u8dl.errors import BrowserError  # noqa: E402
from m3u8dl.websocket_client import WebSocketConnection  # noqa: E402

# プロキシ環境変数の影響を受けずにローカルのデバッグポートを確認するための opener
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# テスト用ページ: Cookie を設定し、プレイリストとセグメントを fetch する
INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>test</title></head>
<body>
<p>browser sniff test page</p>
<script>
document.cookie = 'm3u8dl_test=hello; path=/';
fetch('./test.m3u8')
  .then(function (response) { return response.text(); })
  .then(function () { return fetch('./segment0.ts'); });
</script>
</body>
</html>
"""

# テスト用メディアプレイリスト
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
segment0.ts
#EXTINF:4.0,
segment1.ts
#EXT-X-ENDLIST
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """テスト用の最小 HTTP ハンドラ。"""

    def do_GET(self) -> None:
        """パスに応じて HTML / プレイリスト / セグメントを返す。"""
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', INDEX_HTML.encode('utf-8'))
        elif path == '/test.m3u8':
            self._send(200, 'application/vnd.apple.mpegurl', PLAYLIST.encode('utf-8'))
        elif path == '/segment0.ts':
            # MPEG-TS の同期バイトで始まるダミーセグメント
            self._send(200, 'video/mp2t', b'\x47' * 188)
        else:
            self._send(404, 'text/plain', b'not found')

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        """ステータス・Content-Type・本文を指定してレスポンスを返す。"""
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """テスト出力を汚さないようアクセスログを抑制する。"""
        return


def _start_server() -> tuple[http.server.ThreadingHTTPServer, int]:
    """空きポートでテスト用 HTTP サーバーを起動する。

    Returns:
        (サーバーインスタンス, ポート番号) のタプル。
    """
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, int(server.server_address[1])


def _port_alive(port: int | None) -> bool:
    """指定ポートの CDP エンドポイントが応答するかどうかを返す。

    Args:
        port: 確認するポート番号。

    Returns:
        応答すれば True。
    """
    if not port:
        return False
    try:
        with _OPENER.open(f'http://127.0.0.1:{port}/json/version', timeout=1.0) as response:
            return response.status == 200
    except Exception:
        return False


class TestClassification(unittest.TestCase):
    """URL 種別判定 (純関数) の検証。"""

    def test_playlist(self) -> None:
        """プレイリスト拡張子が playlist に分類されること。"""
        self.assertEqual(_classify('https://example.com/live/master.m3u8?token=abc'), 'playlist')
        self.assertEqual(_classify('https://example.com/audio.m3u'), 'playlist')
        self.assertEqual(_classify('https://example.com/dash/manifest.mpd'), 'playlist')

    def test_segment(self) -> None:
        """セグメント拡張子が segment に分類されること。"""
        self.assertEqual(_classify('https://example.com/seg0.ts'), 'segment')
        self.assertEqual(_classify('https://example.com/chunk.m4s'), 'segment')
        self.assertEqual(_classify('https://example.com/video.mp4'), 'segment')
        self.assertEqual(_classify('https://example.com/audio.aac'), 'segment')

    def test_key_and_other(self) -> None:
        """鍵とそれ以外の分類が正しいこと。"""
        self.assertEqual(_classify('https://example.com/key.bin.key'), 'key')
        self.assertEqual(_classify('https://example.com/app.js'), 'other')
        self.assertEqual(_classify('https://example.com/index.html'), 'other')


class TestWebSocketConnection(unittest.TestCase):
    """WebSocketConnection の異常系の検証。"""

    def test_connect_to_closed_port_raises(self) -> None:
        """接続できないポートでは BrowserError になること。"""
        # 使われていないポートを確保してから閉じ、接続を試みる
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            port = int(probe.getsockname()[1])
        with self.assertRaises(BrowserError):
            WebSocketConnection('127.0.0.1', port, '/', timeout=1.0)


class TestBrowserSnifferLifecycle(unittest.TestCase):
    """実際にブラウザを起動しての統合検証 (ローカル HTTP のみ)。"""

    def test_full_lifecycle(self) -> None:
        """起動 → プレイリスト/セグメント検出 → Cookie/タイトル取得 → 停止を検証する。"""
        server, port = _start_server()
        base_url = f'http://127.0.0.1:{port}'
        requests: list = []
        lock = threading.Lock()

        def on_request(request: object) -> None:
            """検出結果をスレッド安全に蓄積する。"""
            with lock:
                requests.append(request)

        sniffer = BrowserSniffer(on_request=on_request, headless=False)
        debug_port: int | None = None
        try:
            # 1) ブラウザを起動してローカルページを開く
            sniffer.start(f'{base_url}/index.html')
            self.assertTrue(sniffer.is_running(), 'ブラウザが起動していない')
            browser_path = sniffer.get_browser_path()
            self.assertTrue(os.path.isfile(browser_path), f'ブラウザの実体が無い: {browser_path}')
            debug_port = sniffer._port

            # 2) kind='playlist' の URL が届くのを最大 25 秒待つ
            playlist = None
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                with lock:
                    playlist = next(
                        (r for r in requests if r.kind == 'playlist' and 'test.m3u8' in r.url),
                        None,
                    )
                if playlist is not None:
                    break
                time.sleep(0.2)
            self.assertIsNotNone(
                playlist,
                f'playlist を検出できなかった: {[(r.kind, r.url) for r in requests]}',
            )

            # 3) セグメントとその他も分類されて通知されること
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                with lock:
                    kinds = {r.kind for r in requests}
                if 'segment' in kinds:
                    break
                time.sleep(0.2)
            self.assertIn('segment', kinds, f'segment を検出できなかった: {kinds}')
            self.assertIn('other', kinds, 'document などが other として通知されていない')

            # 4) document.cookie が取得できること
            cookies: list = []
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                cookies = sniffer.get_cookies()
                if any(c.get('name') == 'm3u8dl_test' for c in cookies):
                    break
                time.sleep(0.3)
            self.assertTrue(
                any(c.get('name') == 'm3u8dl_test' for c in cookies),
                f'Cookie が取得できない: {cookies}',
            )
            scoped = sniffer.get_cookies(base_url)
            self.assertTrue(
                any(c.get('name') == 'm3u8dl_test' for c in scoped),
                f'URL 指定の Cookie が取得できない: {scoped}',
            )

            # 5) ページタイトルが 'test' であること
            title = ''
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                title = sniffer.get_page_title()
                if title == 'test':
                    break
                time.sleep(0.3)
            self.assertEqual(title, 'test', f'ページタイトルが想定と違う: {title!r}')

            # 6) navigate でも同じターゲットを操作できること (例外が出なければよい)
            sniffer.navigate(f'{base_url}/index.html')
        finally:
            sniffer.stop()
            server.shutdown()
            server.server_close()

        # 7) stop() 後にプロセスとソケットが残っていないこと
        self.assertFalse(sniffer.is_running(), 'stop 後も is_running() が True')
        self.assertIsNone(sniffer._proc, 'stop 後もプロセス参照が残っている')
        self.assertFalse(_port_alive(debug_port), 'stop 後もデバッグポートが生きている')


class TestBrowserFailureDiagnostics(unittest.TestCase):
    """ブラウザ起動失敗時の診断情報とフォールバックの検証。"""

    def test_wait_for_debugger_reports_exit_code_and_log(self) -> None:
        """ブラウザが即終了したとき、終了コードと起動ログの末尾がメッセージに含まれること。"""

        class _FakeProc:
            """即座に終了したブラウザプロセスの代役。"""

            returncode = 3

            def poll(self) -> int:
                """終了済みとして終了コードを返す。"""
                return 3

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / 'launch.log'
            log_path.write_text('boom: cannot create profile', encoding='utf-8')

            sniffer = BrowserSniffer(on_request=lambda request: None)
            sniffer._proc = _FakeProc()  # type: ignore[assignment]
            sniffer._launch_log = log_path
            try:
                with self.assertRaises(BrowserError) as context:
                    sniffer._wait_for_debugger(0)
            finally:
                sniffer._proc = None

            message = str(context.exception)
            self.assertIn('exit code=3', message)
            self.assertIn('boom: cannot create profile', message)

    def test_start_reports_every_candidate(self) -> None:
        """すべての候補で失敗したとき、候補ごとの理由がまとめて報告されること。"""
        import m3u8dl.browser as browser_module

        with tempfile.TemporaryDirectory() as tmp:
            # 「実在するが実行できないファイル」を候補にして起動失敗を再現する
            broken = Path(tmp) / 'broken_browser.exe'
            broken.write_text('not an executable', encoding='utf-8')
            original = browser_module._browser_candidates
            browser_module._browser_candidates = lambda: [str(broken)]
            try:
                sniffer = BrowserSniffer(on_request=lambda request: None)
                with self.assertRaises(BrowserError) as context:
                    sniffer.start('about:blank')
            finally:
                browser_module._browser_candidates = original
                sniffer.stop()

            message = str(context.exception)
            self.assertIn('broken_browser.exe', message)
            self.assertIn('ブラウザ解析を開始できませんでした', message)

    def test_falls_back_to_next_available_browser(self) -> None:
        """1 つ目の候補が起動できなくても、次の候補で解析が開始されること。"""
        import m3u8dl.browser as browser_module

        available = [path for path in browser_module._browser_candidates() if os.path.isfile(path)]
        if not available:
            self.skipTest('対応ブラウザが見つからないためスキップ')

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'missing_browser.exe'
            original = browser_module._browser_candidates
            # 1 つ目は存在しないパス、2 つ目に実在するブラウザを並べる
            browser_module._browser_candidates = lambda: [str(missing), available[0]]
            server, port = _start_server()
            sniffer = BrowserSniffer(on_request=lambda request: None, headless=True)
            try:
                sniffer.start(f'http://127.0.0.1:{port}/index.html')
                self.assertTrue(sniffer.is_running(), 'フォールバック後に解析が開始されていない')
                self.assertEqual(sniffer.get_browser_path(), available[0])
            finally:
                browser_module._browser_candidates = original
                sniffer.stop()
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
