"""CDP ブラウザ解析モジュール (browser.py / websocket_client.py) の実測検証テスト。

外部サイトには一切依存せず、127.0.0.1 で起動した一時 HTTP サーバーだけを使う。
ローカルの `index.html` が `fetch('./test.m3u8')` と `fetch('./segment0.ts')` を実行し、
`BrowserSniffer` がブラウザを実際に起動してそれらを検出できることを確認する。
"""

from __future__ import annotations

import http.server
import os
import subprocess
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

# クリックされるまでプレイリストを取得しないページ (再生ボタンを押して再生するプレーヤーの模擬)。
# 再生の自動操作 (drive) が無いと通信が発生しないため、drive の効果を実測で切り分けられる。
CLICK_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>click to play</title></head>
<body>
<p>click the button to start playback</p>
<button id="play" class="play-button" aria-label="Play video">Play</button>
<script>
document.getElementById('play').addEventListener('click', function () {
  fetch('./test.m3u8')
    .then(function (response) { return response.text(); })
    .then(function () { return fetch('./segment0.ts'); });
});
</script>
</body>
</html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """テスト用の最小 HTTP ハンドラ。"""

    def do_GET(self) -> None:
        """パスに応じて HTML / プレイリスト / セグメントを返す。"""
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', INDEX_HTML.encode('utf-8'))
        elif path == '/click.html':
            self._send(200, 'text/html; charset=utf-8', CLICK_HTML.encode('utf-8'))
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


class TestPlaybackDriveWithRealBrowser(unittest.TestCase):
    """再生の自動操作 (drive) を実ブラウザで検証する (ローカル HTTP のみ)。

    「クリックするまで通信しないページ」を使い、drive があるときだけ検出できることを実測する。
    """

    def _collect(
        self, page_path: str, drive_playback: bool, wait_seconds: float, wait_status: str | None = None
    ) -> tuple[list, list[str]]:
        """ページを解析し、検出したリクエストと状態メッセージを返す。

        Args:
            page_path: 開くページのパス (/click.html など)。
            drive_playback: 再生の自動操作を行うかどうか。
            wait_seconds: 検出を待つ最大時間 (秒)。
            wait_status: 指定した接頭辞の状態メッセージが出るまで待つ (None なら待たない)。

        Returns:
            (検出したリクエストのリスト, 状態メッセージのリスト)。
        """
        server, port = _start_server()
        base_url = f'http://127.0.0.1:{port}'
        requests: list = []
        statuses: list[str] = []
        lock = threading.Lock()

        def on_request(request: object) -> None:
            """検出結果をスレッド安全に蓄積する。

            Args:
                request: SniffedRequest。
            """
            with lock:
                requests.append(request)

        sniffer = BrowserSniffer(
            on_request=on_request,
            on_status=statuses.append,
            headless=True,
            drive_playback=drive_playback,
        )
        try:
            sniffer.start(f'{base_url}{page_path}')
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                with lock:
                    found = any(r.kind == 'playlist' and 'test.m3u8' in r.url for r in requests)
                # 再生の自動操作の報告も待つ。報告は操作の直後に出るが、
                # 速く通信が取れた場合に報告を取りこぼさないようにするため。
                reported = wait_status is None or any(m.startswith(wait_status) for m in statuses)
                if found and reported:
                    break
                time.sleep(0.2)
        finally:
            sniffer.stop()
            server.shutdown()
            server.server_close()
        return requests, statuses

    def test_drive_clicks_play_button_and_detects_playlist(self) -> None:
        """クリックしないと通信しないページでも、自動操作で再生が始まり検出できること。"""
        requests, statuses = self._collect(
            '/click.html',
            drive_playback=True,
            wait_seconds=40.0,
            wait_status='ページの再生操作を自動で行いました',
        )

        urls = [r.url for r in requests]
        self.assertTrue(
            any(r.kind == 'playlist' and 'test.m3u8' in r.url for r in requests),
            f'自動操作で再生が始まらずプレイリストを検出できなかった: {urls}\n状態: {statuses}',
        )
        self.assertTrue(
            any(message.startswith('ページの再生操作を自動で行いました') for message in statuses),
            f'自動操作の実行が報告されていない: {statuses}',
        )

    def test_without_drive_click_page_never_loads_playlist(self) -> None:
        """自動操作を切ると、同じページではプレイリストが検出されないこと (切り分けの対照)。"""
        requests, _ = self._collect('/click.html', drive_playback=False, wait_seconds=12.0)

        self.assertFalse(
            any(r.kind == 'playlist' and 'test.m3u8' in r.url for r in requests),
            f'操作していないのにプレイリストが検出された: {[r.url for r in requests]}',
        )


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


class TestBrowserLaunchResilience(unittest.TestCase):
    """起動直後にプロセスが終了しても解析を続行できることと、起動バリエーションの検証。"""

    def test_wait_for_debugger_survives_process_exit_when_port_answers(self) -> None:
        """プロセスが終了してもデバッグポートが応答すれば起動成功として扱うこと。

        ブラウザは更新の適用や処理の引き継ぎで、起動直後のプロセスだけが終了することがある。
        プロセスの終了だけで即失敗にすると、その後にポートが応答しても解析を始められない。
        """

        class _ExitedProc:
            """終了済みのブラウザプロセスの代役。"""

            returncode = 0

            def poll(self) -> int:
                """終了済みとして 0 を返す。"""
                return 0

        import m3u8dl.browser as browser_module

        statuses: list[str] = []
        calls = {'count': 0}
        original = browser_module._http_get_json

        def fake_get_json(port: int, path: str, timeout: float = 1.0) -> object:
            """最初の 2 回は接続失敗、3 回目以降は応答する偽のデバッグポート。

            Args:
                port: デバッグポート。
                path: リクエストパス。
                timeout: タイムアウト (秒)。

            Returns:
                3 回目以降は /json/version 相当の辞書。

            Raises:
                OSError: 最初の 2 回は接続失敗として送出する。
            """
            calls['count'] += 1
            if calls['count'] < 3:
                raise OSError('not ready')
            return {'webSocketDebuggerUrl': 'ws://127.0.0.1:1/devtools/page/1'}

        sniffer = BrowserSniffer(on_request=lambda request: None, on_status=statuses.append)
        sniffer._proc = _ExitedProc()  # type: ignore[assignment]
        browser_module._http_get_json = fake_get_json
        try:
            # 例外が出なければ、プロセスの終了後もポートの応答を待って起動成功と判定できている
            sniffer._wait_for_debugger(9222)
            self.assertEqual(sniffer._port, 9222)
        finally:
            browser_module._http_get_json = original
            sniffer._proc = None

        self.assertTrue(
            any('待っています' in status for status in statuses),
            f'プロセス終了後にポートを待つ状態メッセージが出ていない: {statuses}',
        )

    def test_launch_variants_fall_back_to_no_sandbox(self) -> None:
        """既定の起動の次に、サンドボックスを無効化した起動が用意されていること。"""
        from m3u8dl.browser import _launch_variants

        variants = _launch_variants()
        self.assertGreaterEqual(len(variants), 2)
        self.assertEqual(variants[0][0], '既定')
        self.assertEqual(variants[0][1], ())
        self.assertTrue(
            any('--no-sandbox' in arguments for _, arguments in variants),
            f'サンドボックス無効の起動が無い: {variants}',
        )

    def test_start_reports_every_variant(self) -> None:
        """起動に失敗したとき、試した起動バリエーションがすべて報告されること。"""
        import m3u8dl.browser as browser_module

        with tempfile.TemporaryDirectory() as tmp:
            # 「実在するが実行できないファイル」を候補にして起動失敗を再現する
            broken = Path(tmp) / 'broken_browser.exe'
            broken.write_text('not an executable', encoding='utf-8')
            original = browser_module._browser_candidates
            browser_module._browser_candidates = lambda: [str(broken)]
            sniffer = BrowserSniffer(on_request=lambda request: None)
            try:
                with self.assertRaises(BrowserError) as context:
                    sniffer.start('about:blank')
            finally:
                browser_module._browser_candidates = original
                sniffer.stop()

            message = str(context.exception)
            self.assertIn('broken_browser.exe (既定)', message)
            self.assertIn('broken_browser.exe (sandbox 無効)', message)

    def test_stop_is_silent_when_idle(self) -> None:
        """何も動いていないときの stop() は状態メッセージを出さないこと。

        起動失敗の処理では stop() が 2 回呼ばれるため、ログが二重に出ないようにしている。
        """
        statuses: list[str] = []
        sniffer = BrowserSniffer(on_request=lambda request: None, on_status=statuses.append)
        sniffer.stop()
        sniffer.stop()
        self.assertEqual(
            [status for status in statuses if '停止しました' in status],
            [],
            f'停止メッセージが出力された: {statuses}',
        )

    def test_profile_report_notes_missing_profile(self) -> None:
        """一時プロファイルが未作成のとき、その旨が報告されること。"""
        sniffer = BrowserSniffer(on_request=lambda request: None)
        self.assertIn('未作成', sniffer._profile_report())

    def test_creation_flags_detach_from_parent(self) -> None:
        """ブラウザを親から切り離して起動する生成フラグを使うこと。

        親のコンソールやジョブオブジェクトに繋がった状態で起動すると、Chromium 系ブラウザが
        何も出力せずに終了コード 0 で即終了することがあるため、切り離しフラグを必須にしている。
        """
        import m3u8dl.browser as browser_module

        if os.name != 'nt':
            self.skipTest('Windows 専用の検証')
        flags = browser_module._BROWSER_CREATION_FLAGS
        self.assertTrue(flags & subprocess.DETACHED_PROCESS, 'コンソールの切り離しが指定されていない')
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP, '新しいプロセスグループが指定されていない')
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB, 'ジョブオブジェクトからの離脱が指定されていない')
        # 離脱を拒否する環境向けの予備フラグでは、離脱だけを外す
        fallback = browser_module._BROWSER_CREATION_FLAGS_FALLBACK
        self.assertTrue(fallback & subprocess.DETACHED_PROCESS)
        self.assertFalse(fallback & subprocess.CREATE_BREAKAWAY_FROM_JOB)

    def test_spawn_browser_retries_without_breakaway(self) -> None:
        """離脱が拒否されたとき、離脱なしの生成フラグで再試行すること。"""
        from unittest import mock

        if os.name != 'nt':
            self.skipTest('Windows 専用の検証')
        sniffer = BrowserSniffer(on_request=lambda request: None)
        sentinel = object()
        with mock.patch('subprocess.Popen', side_effect=[OSError(5, 'access denied'), sentinel]) as popen:
            proc, used_flags = sniffer._spawn_browser(['chrome.exe', 'about:blank'], None)
        self.assertIs(proc, sentinel)
        self.assertEqual(popen.call_count, 2, '再試行されていない')
        first = popen.call_args_list[0].kwargs['creationflags']
        second = popen.call_args_list[1].kwargs['creationflags']
        self.assertNotEqual(first, second, '同じ生成フラグで再試行している')
        self.assertEqual(used_flags, second)

    def test_spawn_browser_reports_failure(self) -> None:
        """どの生成フラグでも起動できなければ BrowserError になること。"""
        from unittest import mock

        sniffer = BrowserSniffer(on_request=lambda request: None)
        with mock.patch('subprocess.Popen', side_effect=OSError(5, 'access denied')):
            with self.assertRaises(BrowserError) as context:
                sniffer._spawn_browser(['chrome.exe', 'about:blank'], None)
        self.assertIn('ブラウザの起動に失敗しました', str(context.exception))


class TestBrowserSelectionFromCli(unittest.TestCase):
    """`--browser` 引数からブラウザを指定できることの検証 (引数の解釈のみ)。"""

    def test_resolves_browser_name_and_path(self) -> None:
        """ブラウザ名と実行ファイルのパスの両方を受け付けること。"""
        import main as cli

        # 実行ファイルのパスはそのまま使う
        executable = str(Path(sys.executable))
        self.assertEqual(cli._resolve_browser_argument(executable), executable)
        # 未知の名前は解決できない
        self.assertIsNone(cli._resolve_browser_argument('netscape'))

        # ブラウザ名は、そのファミリーの実行ファイル名 (chrome.exe / msedge.exe / brave.exe) に解決される
        resolved_names: list[tuple[str, str]] = []
        for name in ('chrome', 'edge', 'brave'):
            resolved = cli._resolve_browser_argument(name)
            self.assertTrue(resolved is None or os.path.isfile(resolved), f'{name} の解決結果が不正: {resolved}')
            if resolved is not None:
                resolved_names.append((name, os.path.basename(resolved).lower()))
        if not resolved_names:
            self.skipTest('対応ブラウザが見つからないためスキップ')
        for name, executable_name in resolved_names:
            self.assertIn(executable_name, cli._BROWSER_NAME_MAP[name])


if __name__ == '__main__':
    unittest.main()
