"""GUI の解析モード (バックグラウンド / 画面あり) の検証テスト。

`BrowserSniffer` を差し替えて、GUI が実際にどの起動モードを選ぶかを実測する。
ブラウザは起動しないため、外部サイトにも依存しない。
"""

from __future__ import annotations

import gc
import sys
import tkinter as tk
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl import gui  # noqa: E402


class _FakeSniffer:
    """BrowserSniffer の代わりに呼び出し内容を記録する偽物。"""

    # starts: これまでに start() が呼ばれた回数 (クラス変数なのでインスタンスをまたいで数えられる)
    starts = 0
    # last_kwargs: 直近のインスタンス生成に使われたキーワード引数
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        """呼び出し内容を記録する。

        Args:
            **kwargs: GUI から渡された引数。
        """
        _FakeSniffer.last_kwargs = dict(kwargs)

    def start(self, url: str) -> None:
        """解析開始の呼び出しを記録する。

        Args:
            url: 解析対象の URL。
        """
        _FakeSniffer.starts += 1

    def stop(self) -> None:
        """停止呼び出し (何もしない)。"""


class AnalysisModeTest(unittest.TestCase):
    """解析時にブラウザの画面を出すかどうかの既定と切り替え。"""

    def _build_app(self) -> tuple[Any, Any]:
        """テスト用に GUI を構築する (ウィンドウは表示しない)。

        Returns:
            (root, app) のタプル。
        """
        root = tk.Tk()
        root.withdraw()
        app = gui.DownloaderApp(root)
        return root, app

    def test_default_is_background(self) -> None:
        """既定ではブラウザの画面を出さない (バックグラウンドで解析する) こと。"""
        root, app = self._build_app()
        try:
            self.assertFalse(app.show_browser_var.get(), '解析中の画面表示が既定で有効になっている')
            self.assertIsNotNone(app.show_browser_check)
        finally:
            root.destroy()
            gc.collect()

    def test_run_analysis_uses_headless_in_background(self) -> None:
        """画面を出さない設定では、ヘッドレス (バックグラウンド) で起動すること。"""
        root, app = self._build_app()
        _FakeSniffer.last_kwargs = {}
        try:
            with mock.patch.object(gui, 'BrowserSniffer', _FakeSniffer):
                app._run_analysis('https://example.com/page', {}, show_window=False)
        finally:
            root.destroy()
            gc.collect()

        self.assertTrue(_FakeSniffer.last_kwargs.get('headless'), f'ヘッドレスで起動していない: {_FakeSniffer.last_kwargs}')

    def test_run_analysis_shows_window_when_requested(self) -> None:
        """画面を出す設定では、ヘッドレスにしないこと (ページ操作が必要なサイト向け)。"""
        root, app = self._build_app()
        _FakeSniffer.last_kwargs = {}
        try:
            with mock.patch.object(gui, 'BrowserSniffer', _FakeSniffer):
                app._run_analysis('https://example.com/page', {}, show_window=True)
        finally:
            root.destroy()
            gc.collect()

        self.assertFalse(_FakeSniffer.last_kwargs.get('headless'), f'画面ありで起動していない: {_FakeSniffer.last_kwargs}')

    def test_analyze_button_passes_checkbox_state(self) -> None:
        """「ブラウザで解析」がチェックボックスの状態を起動モードへ反映すること。"""
        root, app = self._build_app()
        _FakeSniffer.starts = 0
        _FakeSniffer.last_kwargs = {}
        done: dict[str, bool] = {'finished': False}

        def tick() -> None:
            """解析スレッドがスニッファを起動するまで待つポーリング。"""
            if _FakeSniffer.starts > 0:
                done['finished'] = True
                # 解析を止めてから終わる (バックグラウンドスレッドを残さない)
                app._on_stop_analysis()
                root.quit()
                return
            if not app.busy:
                root.quit()
                return
            root.after(100, tick)

        try:
            with mock.patch.object(gui, 'BrowserSniffer', _FakeSniffer):
                app.url_var.set('https://example.com/page')
                app.show_browser_var.set(True)
                root.after(100, lambda: (app._on_analyze(), root.after(100, tick)))
                # フリーズさせないための安全弁
                root.after(20000, root.quit)
                root.mainloop()
                self.assertTrue(done['finished'], '解析が開始されなかった')
                self.assertFalse(_FakeSniffer.last_kwargs.get('headless'), 'チェックの状態が反映されていない')
        finally:
            del app
            gc.collect()
            root.destroy()
            gc.collect()


if __name__ == '__main__':
    unittest.main()
