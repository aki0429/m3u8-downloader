"""GUI (DownloaderApp) の「ブラウザで解析」経路を実際に動かす end-to-end テスト。

ローカルの HTTP サーバーに置いたページを GUI から解析させ、ブラウザ (CDP) を含む解析経路で
プレイリストが一覧に反映され、バリアントが列挙され、停止できるところまでを確認する。
対象は GUI と BrowserSniffer の結線 (ワーカースレッド・コールバック・ツリー反映・停止処理) で、
外部サイトには依存しない。

ワーカースレッドからの `root.after` は「メインスレッドが mainloop の中にいる」ときだけ許可される
(tkinter はそれ以外のスレッドからの Tcl 呼び出しを RuntimeError で拒否する)。そのためテストは
実アプリと同じく mainloop() を回し、検証は root.after で組み立てたポーリングで行う。
"""

from __future__ import annotations

import functools
import gc
import http.server
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

try:  # pragma: no cover - 環境依存
    import tkinter as tk

    _TK_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - tkinter が無い環境
    tk = None  # type: ignore[assignment]
    _TK_ERROR = exc

# 解析対象のページ: プレイリストを fetch して、ブラウザに m3u8 を要求させる
INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>analysis test</title></head>
<body>
<p>gui analysis test page</p>
<script>
fetch('./master.m3u8').then(function (response) { return response.text(); });
</script>
</body>
</html>
"""

# マスタープレイリスト: バリアントが 2 つあるので画質の列挙まで検証できる
MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
high.m3u8
"""

# メディアプレイリスト (バリアントの実体)
MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
segment0.ts
#EXT-X-ENDLIST
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    """解析用のページとプレイリストを返す最小の HTTP ハンドラ。"""

    def do_GET(self) -> None:  # noqa: N802 - 親クラスのメソッド名に合わせる
        """パスに応じて HTML / プレイリスト / セグメントを返す。"""
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            self._send('text/html; charset=utf-8', INDEX_HTML)
        elif path == '/master.m3u8':
            self._send('application/vnd.apple.mpegurl', MASTER_PLAYLIST)
        elif path in ('/low.m3u8', '/high.m3u8'):
            self._send('application/vnd.apple.mpegurl', MEDIA_PLAYLIST)
        else:
            self._send('text/plain', 'not found', status=404)

    def _send(self, content_type: str, body: str, status: int = 200) -> None:
        """ステータス・Content-Type・本文を指定してレスポンスを返す。

        Args:
            content_type: Content-Type ヘッダの値。
            body: レスポンス本文。
            status: HTTP ステータスコード。
        """
        raw = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 親のシグネチャに合わせる
        """テスト出力を汚さないようアクセスログを捨てる。

        Args:
            format: 書式文字列 (未使用)。
            *args: 書式引数 (未使用)。
        """
        return


class GuiAnalysisEndToEndTest(unittest.TestCase):
    """GUI の「ブラウザで解析」経路を実測する。"""

    httpd: http.server.ThreadingHTTPServer
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """ローカルの HTTP サーバーを起動する。"""
        if tk is None:
            raise unittest.SkipTest(f'tkinter が使えないためスキップ: {_TK_ERROR}')
        handler = functools.partial(_Handler)
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base_url = f'http://127.0.0.1:{cls.httpd.server_address[1]}'

    @classmethod
    def tearDownClass(cls) -> None:
        """HTTP サーバーを片付ける。"""
        if getattr(cls, 'httpd', None) is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()

    def _drive_analysis(self, page_url: str, timeout: float = 90.0) -> dict[str, Any]:
        """GUI を実際の mainloop で走らせてブラウザ解析を行い、停止まで進める。

        Args:
            page_url: 解析対象のページ URL。
            timeout: 打ち切り時間 (秒)。

        Returns:
            'result' (detected / not_busy / timeout) / 'log' / 'urls' / 'variants' /
            'dialogs' / 'stop_error' を持つ辞書。
        """
        from m3u8dl import gui

        # ダイアログが開くと mainloop が止まってテストが固まるため、記録だけして即座に閉じる
        dialogs: list[tuple[str, tuple[Any, ...]]] = []
        original_show_error = gui.messagebox.showerror
        original_show_warning = gui.messagebox.showwarning
        original_show_info = gui.messagebox.showinfo
        gui.messagebox.showerror = lambda *args, **kwargs: dialogs.append(('error', args))
        gui.messagebox.showwarning = lambda *args, **kwargs: dialogs.append(('warning', args))
        gui.messagebox.showinfo = lambda *args, **kwargs: dialogs.append(('info', args))

        root = tk.Tk()
        root.withdraw()  # 画面には出さない
        try:
            app = gui.DownloaderApp(root)
            state: dict[str, Any] = {'result': None, 'log': '', 'urls': [], 'variants': [], 'dialogs': dialogs}

            def finish(reason: str) -> None:
                """解析を停止して mainloop を抜ける。

                Args:
                    reason: 打ち切り理由 (detected / not_busy / timeout)。
                """
                state['result'] = reason
                try:
                    if app.sniffer is not None:
                        app._on_stop_analysis()
                except Exception as exc:  # 停止時の例外は検証側で報告する
                    state['stop_error'] = repr(exc)
                root.quit()

            def tick() -> None:
                """プレイリストとバリアントが揃うまでポーリングする。"""
                urls = list(app.tree_items)
                # マスタープレイリストの行が出て、バリアントの列挙も終わったら成功とみなす
                if any(url.endswith('master.m3u8') for url in urls) and app.variants:
                    finish('detected')
                    return
                # busy が下りているのに検出できていない場合は解析自体が失敗している
                if not app.busy and not any(url.endswith('.m3u8') for url in urls):
                    finish('not_busy')
                    return
                root.after(200, tick)

            def start() -> None:
                """「ブラウザで解析」を押したのと同じ状態にする。"""
                app.url_var.set(page_url)
                app._on_analyze()
                root.after(200, tick)

            root.after(200, start)
            # 打ち切り用の安全弁 (フリーズさせない)
            root.after(int(timeout * 1000), lambda: finish('timeout'))
            root.mainloop()

            state['log'] = app.log_text.get('1.0', 'end')
            state['urls'] = list(app.tree_items)
            state['variants'] = list(app.variants)
            state['busy'] = app.busy
            # Tk 変数は root より先に解放する (終了時の Tcl エラーを避ける)
            del app
            gc.collect()
            return state
        finally:
            root.destroy()
            gc.collect()
            gui.messagebox.showerror = original_show_error
            gui.messagebox.showwarning = original_show_warning
            gui.messagebox.showinfo = original_show_info

    def test_gui_analysis_detects_playlist_and_variants(self) -> None:
        """GUI の「ブラウザで解析」経路でプレイリストが一覧に入り、バリアントが列挙されること。"""
        state = self._drive_analysis(f'{self.base_url}/index.html')

        self.assertEqual(state['dialogs'], [], f'想定外のダイアログ: {state["dialogs"]}')
        self.assertEqual(
            state['result'],
            'detected',
            f'解析が完了しなかった ({state["result"]}): {state["urls"]}\n{state["log"]}',
        )
        self.assertTrue(
            any(url.endswith('/master.m3u8') for url in state['urls']),
            f'マスタープレイリストが一覧に無い: {state["urls"]}',
        )
        self.assertIn('ブラウザ解析を開始しました', state['log'])
        self.assertIn('ブラウザ解析を停止しました', state['log'])
        self.assertIsNone(state.get('stop_error'), f'停止処理で例外: {state.get("stop_error")}')
        self.assertFalse(state['busy'], '停止後も busy が下りていない')
        # マスタープレイリストの解析結果が画質の一覧へ反映されていること
        labels = [variant.label for variant in state['variants']]
        self.assertEqual(len(labels), 2, f'バリアント数が想定と違う: {labels}')


if __name__ == '__main__':
    unittest.main()
