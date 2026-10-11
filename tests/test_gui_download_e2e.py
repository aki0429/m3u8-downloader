"""GUI (DownloaderApp) から実際にダウンロードを実行する end-to-end テスト。

tkinter のウィンドウを表示せずに DownloaderApp を生成し、入力欄へプレイリスト URL と保存先を
設定して「ダウンロード開始」と同じ経路 (_on_start_download) を呼び、完成したファイルを ffprobe で
検証する。GUI とダウンローダーの結線 (ヘッダ・オプション・スレッド処理・進捗反映) が対象。

ワーカースレッドからの `root.after` は「メインスレッドが mainloop の中にいる」ときだけ許可される
(tkinter はそれ以外のスレッドからの Tcl 呼び出しを RuntimeError で拒否する)。そのためテストは
実アプリと同じく mainloop() を回し、検証は root.after で組み立てたポーリングで行う。
"""

from __future__ import annotations

import functools
import gc
import http.server
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
# ローカル HLS サーバーのフィクスチャ (ffmpeg でのテスト素材生成) を再利用する
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')

try:  # pragma: no cover - 環境依存
    import tkinter as tk

    _TK_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - tkinter が無い環境
    tk = None  # type: ignore[assignment]
    _TK_ERROR = exc


class _ThrottledHandler(http.server.SimpleHTTPRequestHandler):
    """セグメント (.ts) の応答をわざと遅らせるハンドラー (キャンセル検証用)。"""

    # 1 リクエストあたりの遅延 (秒)。キャンセルが間に合う程度に遅くする
    delay_seconds = 0.6

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 親のシグネチャに合わせる
        """アクセスログを捨てる。

        Args:
            format: 書式文字列 (未使用)。
            *args: 書式引数 (未使用)。
        """
        return

    def do_GET(self) -> None:  # noqa: N802 - 親クラスのメソッド名に合わせる
        """セグメント要求のときだけ遅延させてから応答する。"""
        if self.path.endswith('.ts'):
            time.sleep(self.delay_seconds)
        super().do_GET()


class GuiDownloadEndToEndTest(unittest.TestCase):
    """GUI の操作から保存完了までを実測する。"""

    tmp_root: Path
    base_url: str
    httpd: http.server.ThreadingHTTPServer

    @classmethod
    def setUpClass(cls) -> None:
        """ffmpeg でテスト素材を生成し、HTTP サーバー (遅延つき) を起動する。"""
        if FFMPEG is None or FFPROBE is None:
            raise unittest.SkipTest('ffmpeg / ffprobe が見つからないためスキップ')
        if tk is None:
            raise unittest.SkipTest(f'tkinter が使えないためスキップ: {_TK_ERROR}')

        # 実素材の生成は統合テストのフィクスチャを再利用する (重複実装を避ける)
        from test_integration_local_hls import LocalHLSIntegrationTest

        cls._fixture = LocalHLSIntegrationTest
        LocalHLSIntegrationTest.setUpClass()
        cls.tmp_root = LocalHLSIntegrationTest.tmp_root

        # キャンセルを確実に間に合わせるため、.ts 応答を遅延させるサーバーを自前で立てる
        handler = functools.partial(_ThrottledHandler, directory=str(cls.tmp_root))
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base_url = f'http://127.0.0.1:{cls.httpd.server_address[1]}'

    @classmethod
    def tearDownClass(cls) -> None:
        """HTTP サーバーと一時ディレクトリを片付ける。"""
        if getattr(cls, 'httpd', None) is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        if getattr(cls, '_fixture', None) is not None:
            cls._fixture.tearDownClass()

    def _drive_gui(
        self,
        configure: Callable[[Any], None],
        on_tick: Callable[[Any], None] | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        """GUI を実際の mainloop で走らせてダウンロードを終わらせる。

        Args:
            configure: 生成直後の DownloaderApp に URL やオプションを設定する関数。
            on_tick: 150ms ごとに呼ばれる追加処理 (キャンセル要求などに使う)。
            timeout: 打ち切り時間 (秒)。

        Returns:
            'completed' (busy が下りて終わったか) / 'log' (ログ全文) /
            'status' (ステータスラベル) / 'dialogs' (抑止したダイアログの記録) を持つ辞書。
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
            configure(app)
            state: dict[str, Any] = {'completed': False, 'log': '', 'status': '', 'dialogs': dialogs}

            def start() -> None:
                """「ダウンロード開始」を押したのと同じ状態にする。"""
                app._on_start_download()
                root.after(150, tick)

            def tick() -> None:
                """busy が下りるまでポーリングする。"""
                if on_tick is not None:
                    on_tick(app)
                if app.busy:
                    root.after(150, tick)
                    return
                state['completed'] = True
                root.quit()

            root.after(200, start)
            # 打ち切り用の安全弁 (フリーズさせない)
            root.after(int(timeout * 1000), root.quit)
            root.mainloop()

            state['log'] = app.log_text.get('1.0', 'end')
            state['status'] = str(app.status_label.cget('text'))
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

    def test_gui_start_download_writes_valid_mp4(self) -> None:
        """GUI の「ダウンロード開始」経路で有効な mp4 が保存されること。"""
        from m3u8dl.gui import _QUALITY_BEST, DownloaderApp

        output_path = self.tmp_root / 'gui_out.mp4'
        url = f'{self.base_url}/plain/out.m3u8'

        def configure(app: DownloaderApp) -> None:
            """URL・保存先・オプションを GUI の入力欄に設定する。"""
            app.url_var.set(url)
            app.output_var.set(str(output_path))
            app.quality_var.set(_QUALITY_BEST)
            app.concurrency_var.set(4)
            app.convert_var.set(True)
            app.live_var.set(False)
            app.max_segments_var.set('')

        state = self._drive_gui(configure)

        self.assertEqual(state['dialogs'], [], f'想定外のダイアログ: {state["dialogs"]}')
        self.assertTrue(state['completed'], 'ダウンロードが制限時間内に終わりませんでした')
        self.assertIn('ダウンロードが完了しました', state['log'])
        self.assertTrue(output_path.exists(), 'mp4 が生成されていない')
        self.assertGreater(output_path.stat().st_size, 50_000, 'mp4 のサイズが小さすぎる')

        # ffprobe で実際に再生可能な mp4 であることを確認する
        probe = subprocess.run(
            [
                str(FFPROBE), '-v', 'error',
                '-show_entries', 'format=format_name,duration',
                '-show_entries', 'stream=codec_type',
                '-of', 'default=nw=1', str(output_path),
            ],
            capture_output=True, text=True, timeout=60.0,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn('mov,mp4', probe.stdout)
        self.assertIn('codec_type=video', probe.stdout)
        self.assertIn('codec_type=audio', probe.stdout)
        # ステータスラベルまで完了状態が反映されていること (進捗の UI 反映の確認)
        self.assertIn('完了', state['status'])

    def test_gui_cancel_stops_download(self) -> None:
        """GUI のキャンセル操作でダウンロードが中断されること。"""
        from m3u8dl.gui import DownloaderApp

        output_path = self.tmp_root / 'gui_cancel.mp4'
        url = f'{self.base_url}/aes/enc.m3u8'
        requested: list[bool] = []

        def configure(app: DownloaderApp) -> None:
            """AES 配信を 1 並列でダウンロードするよう設定する。"""
            app.url_var.set(url)
            app.output_var.set(str(output_path))
            app.concurrency_var.set(1)

        def on_tick(app: DownloaderApp) -> None:
            """ダウンローダーが動き始めたら 1 度だけキャンセルを要求する。"""
            if requested or app.downloader is None:
                return
            requested.append(True)
            app._on_cancel()

        state = self._drive_gui(configure, on_tick=on_tick, timeout=60.0)

        self.assertTrue(requested, 'ダウンローダーが起動する前に終わってしまいました')
        self.assertTrue(state['completed'], 'キャンセル後に busy が下りませんでした')
        self.assertIn('キャンセル', state['log'])


if __name__ == '__main__':
    unittest.main()
