"""再生の自動操作 (CDP によるページ操作) の検証テスト。

ブラウザは起動せず、CDP コマンドのやり取りを偽装して検証する。
`BrowserSniffer` が「ページを開いて待つだけ」ではなく、実際に再生を始めさせて
そのときの通信を捕まえにいっていることを実測で確認する。
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl import browser as browser_module  # noqa: E402
from m3u8dl.browser import BrowserSniffer, _PLAY_DRIVE_SCRIPT  # noqa: E402
from m3u8dl.errors import BrowserError  # noqa: E402

# メインフレーム + 入れ子の iframe を含むフレームツリー (実測用の偽応答)
FRAME_TREE = {
    'frameTree': {
        'frame': {'id': 'main'},
        'childFrames': [
            {'frame': {'id': 'child1'}, 'childFrames': [{'frame': {'id': 'grandchild'}}]},
            {'frame': {'id': 'child2'}},
        ],
    }
}

# 再生操作スクリプトの戻り値 (動画 1 件・play() 成功・再生ボタンのクリック 2 件)
PLAY_DRIVE_RESULT = '{"media": 1, "played": 1, "clicked": 2}'
# 停止中で押すべき要素の座標が取れた場合の戻り値 (本物のクリックを送る条件)
PLAY_DRIVE_PAUSED_RESULT = '{"media": 1, "played": 1, "clicked": 1, "paused": true, "x": 120, "y": 340}'
# 既に再生中の場合の戻り値 (この場合は本物のクリックを送らない)
PLAY_DRIVE_PLAYING_RESULT = '{"media": 1, "played": 1, "clicked": 1, "paused": false, "x": 120, "y": 340}'


class _FakeSniffer(BrowserSniffer):
    """CDP コマンドを偽装したスニッファ (ブラウザを起動しない)。"""

    def __init__(self, responses: dict | None = None, **kwargs: object) -> None:
        """偽の応答表を持つスニッファを作る。

        Args:
            responses: CDP メソッド名 → 返す値 (関数なら呼び出して使う)。
            **kwargs: BrowserSniffer へ渡す追加の引数。
        """
        super().__init__(on_request=lambda request: None, **kwargs)  # type: ignore[arg-type]
        self.commands: list[tuple[str, dict]] = []
        self.responses = responses or {}
        self.context_counter = 0

    def _send_command(self, method: str, params: dict | None = None, timeout: float = 10.0) -> object:
        """CDP コマンドを記録し、用意した応答を返す。

        Args:
            method: CDP メソッド名。
            params: パラメータ。
            timeout: 未使用 (偽装のため)。

        Returns:
            用意した応答。

        Raises:
            BrowserError: 応答が用意されていないメソッドのとき。
        """
        self.commands.append((method, dict(params or {})))
        handler = self.responses.get(method)
        if handler is None:
            raise BrowserError(f'想定外の CDP コマンドです: {method}')
        if callable(handler):
            return handler(params)
        return handler

    def _evaluate_calls(self) -> list[dict]:
        """Runtime.evaluate に渡されたパラメータだけを返す。

        Returns:
            Runtime.evaluate のパラメータ辞書のリスト。
        """
        return [params for method, params in self.commands if method == 'Runtime.evaluate']


class PlayDriveScriptTest(unittest.TestCase):
    """再生操作スクリプトの内容を確認する。"""

    def test_script_plays_media_and_clicks_play_buttons(self) -> None:
        """スクリプトが play() の実行と再生ボタンのクリックの両方を行おうとすること。"""
        self.assertIn('play()', _PLAY_DRIVE_SCRIPT)
        self.assertIn('.click()', _PLAY_DRIVE_SCRIPT)
        # 自動再生が弾かれる環境でも止まらないように、失敗した Promise を握りつぶしていること
        self.assertIn('catch', _PLAY_DRIVE_SCRIPT)

    def test_summary_is_console_safe(self) -> None:
        """状態メッセージに使う要約が cp932 で表せる文字だけで作られること。

        windowed exe ではコンソール出力が cp932 になり、表せない文字が混ざると
        `UnicodeEncodeError` で結果を残せずに落ちるため (tests/test_console_encoding.py も参照)。
        """
        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Runtime.evaluate': {'result': {'value': PLAY_DRIVE_RESULT}},
            }
        )

        summary = sniffer._drive_playback_once()

        self.assertIsNotNone(summary)
        assert summary is not None
        # 例外が出なければ cp932 で表せる (状態メッセージとして安全)
        summary.encode('cp932')


class DrivePlaybackOnceTest(unittest.TestCase):
    """`_drive_playback_once()` の実測 (フレームごとの操作)。"""

    def test_evaluates_main_frame_and_nested_frames(self) -> None:
        """メインフレームと入れ子の iframe のすべてでスクリプトが実行されること。"""

        def create_world(params: dict) -> dict:
            """フレームごとに増える実行コンテキスト ID を返す。

            Args:
                params: CDP のパラメータ。

            Returns:
                executionContextId を持つ辞書。
            """
            sniffer.context_counter += 1
            return {'executionContextId': 100 + sniffer.context_counter}

        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': FRAME_TREE,
                'Page.createIsolatedWorld': create_world,
                'Runtime.evaluate': {'result': {'type': 'string', 'value': PLAY_DRIVE_RESULT}},
            }
        )

        summary = sniffer._drive_playback_once()

        # メイン + child1 + grandchild + child2 の 4 回実行される
        calls = sniffer._evaluate_calls()
        self.assertEqual(len(calls), 4, f'実行コンテキストの数が想定と違う: {calls}')
        # 1 回目はメインフレーム (contextId 指定なし)、以降は iframe 用のコンテキストを指定する
        self.assertNotIn('contextId', calls[0])
        for call in calls[1:]:
            self.assertIn('contextId', call)
        # どのフレームでも同じ結果になるため、要約は 1 つに畳まれる
        self.assertEqual(summary, '動画 1 / 再生 1 / クリック 2')

    def test_returns_none_when_page_has_no_media(self) -> None:
        """動画も再生ボタンも無いページでは要約を返さないこと。"""
        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Page.createIsolatedWorld': {'executionContextId': 1},
                'Runtime.evaluate': {'result': {'value': '{"media": 0, "played": 0, "clicked": 0}'}},
            }
        )

        self.assertIsNone(sniffer._drive_playback_once())

    def test_tolerates_cdp_failures(self) -> None:
        """CDP が失敗しても例外を投げずに次の試行へ任せること (解析自体を止めない)。"""

        def fail(params: dict) -> object:
            """必ず失敗させる。

            Args:
                params: CDP のパラメータ。

            Raises:
                BrowserError: 常に送出する。
            """
            raise BrowserError('実行コンテキストが破棄されました')

        # フレームツリーの取得に失敗する場合
        broken_tree = _FakeSniffer(responses={})  # responses が無い = BrowserError
        self.assertIsNone(broken_tree._drive_playback_once())

        # スクリプトの実行だけが失敗する場合
        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Page.createIsolatedWorld': {'executionContextId': 7},
                'Runtime.evaluate': fail,
            }
        )
        self.assertIsNone(sniffer._drive_playback_once())

    def test_ignores_unparsable_result(self) -> None:
        """ページが想定外の値を返しても落ちないこと。"""
        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Runtime.evaluate': {'result': {'type': 'undefined'}},
            }
        )

        self.assertIsNone(sniffer._drive_playback_once())


class TrustedClickTest(unittest.TestCase):
    """CDP の入力イベントによる「本物のクリック」の検証。"""

    def _sniffer_with(self, payload: str) -> _FakeSniffer:
        """指定した戻り値を返す偽スニッファを作る。

        Args:
            payload: 再生操作スクリプトが返す JSON 文字列。

        Returns:
            偽スニッファ。
        """
        return _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Runtime.evaluate': {'result': {'value': payload}},
                'Input.dispatchMouseEvent': {},
            }
        )

    def _input_events(self, sniffer: _FakeSniffer) -> list[dict]:
        """入力イベントとして送られたパラメータを返す。

        Args:
            sniffer: 偽スニッファ。

        Returns:
            Input.dispatchMouseEvent のパラメータ辞書のリスト。
        """
        return [params for method, params in sniffer.commands if method == 'Input.dispatchMouseEvent']

    def test_sends_press_and_release_when_paused(self) -> None:
        """停止中で座標が取れたときは、押して離す操作を送ること。"""
        sniffer = self._sniffer_with(PLAY_DRIVE_PAUSED_RESULT)

        sniffer._drive_playback_once()

        events = self._input_events(sniffer)
        self.assertEqual([event.get('type') for event in events], ['mousePressed', 'mouseReleased'])
        for event in events:
            self.assertEqual((event.get('x'), event.get('y')), (120, 340))
            self.assertEqual(event.get('button'), 'left')

    def test_does_not_click_while_playing(self) -> None:
        """既に再生中のときは本物のクリックを送らないこと (一時停止させないため)。"""
        sniffer = self._sniffer_with(PLAY_DRIVE_PLAYING_RESULT)

        sniffer._drive_playback_once()

        self.assertEqual(self._input_events(sniffer), [])

    def test_does_not_click_without_coordinates(self) -> None:
        """押すべき要素の座標が取れなかったときは何も送らないこと。"""
        sniffer = self._sniffer_with(PLAY_DRIVE_RESULT)

        sniffer._drive_playback_once()

        self.assertEqual(self._input_events(sniffer), [])

    def test_tolerates_input_failure(self) -> None:
        """入力イベントを送れない環境でも例外を投げないこと。"""
        sniffer = _FakeSniffer(
            responses={
                'Page.getFrameTree': {'frameTree': {'frame': {'id': 'main'}}},
                'Runtime.evaluate': {'result': {'value': PLAY_DRIVE_PAUSED_RESULT}},
            }
        )

        # Input.dispatchMouseEvent の応答を用意していない = BrowserError になる
        summary = sniffer._drive_playback_once()

        self.assertEqual(summary, '動画 1 / 再生 1 / クリック 1')


class PlaybackDriveLoopTest(unittest.TestCase):
    """`_playback_drive_loop()` の実測 (繰り返しと状態メッセージ)。"""

    def test_reports_once_and_stops_after_attempts(self) -> None:
        """再生を開始できた旨を 1 回だけ通知し、規定回数で終わること。"""
        statuses: list[str] = []
        sniffer = _FakeSniffer(on_status=statuses.append, drive_playback=True)
        # 稼働中とみなして、ループを規定回数回させる
        with mock.patch.object(sniffer, 'is_running', return_value=True), mock.patch.object(
            sniffer, '_drive_playback_once', return_value='動画 1 / 再生 1 / クリック 1'
        ) as drive:
            with mock.patch.object(browser_module, '_PLAY_DRIVE_INTERVAL_SECONDS', 0.01), mock.patch.object(
                browser_module, '_PLAY_DRIVE_ATTEMPTS', 3
            ):
                sniffer._playback_drive_loop()

        self.assertEqual(drive.call_count, 3, '規定回数だけ試していない')
        reports = [message for message in statuses if message.startswith('ページの再生操作を自動で行いました')]
        self.assertEqual(len(reports), 1, f'通知が 1 回ではない: {statuses}')

    def test_stops_immediately_when_stop_requested(self) -> None:
        """停止が要求されたら待たずに終わること (stop() がすぐ返れるようにする)。"""
        sniffer = _FakeSniffer(drive_playback=True)
        sniffer._drive_stop.set()

        with mock.patch.object(browser_module, '_PLAY_DRIVE_INTERVAL_SECONDS', 5.0), mock.patch.object(
            sniffer, '_drive_playback_once'
        ) as drive:
            started = threading.Event()
            thread = threading.Thread(target=lambda: (sniffer._playback_drive_loop(), started.set()))
            thread.start()
            thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive(), '停止要求後もループが動き続けている')
        self.assertEqual(drive.call_count, 0, '停止要求後もページ操作を試みている')


class StartPlayDriveTest(unittest.TestCase):
    """`_start_playback_drive()` の起動条件を確認する。"""

    def test_not_started_when_disabled(self) -> None:
        """drive_playback=False ではスレッドを起こさないこと。"""
        sniffer = _FakeSniffer(drive_playback=False)

        sniffer._start_playback_drive()

        self.assertIsNone(sniffer._drive_thread)

    def test_started_and_stopped_with_stop(self) -> None:
        """既定では自動操作のスレッドが起動し、stop() で確実に終わること。"""
        sniffer = _FakeSniffer(drive_playback=True)
        with mock.patch.object(browser_module, '_PLAY_DRIVE_INTERVAL_SECONDS', 0.05), mock.patch.object(
            sniffer, 'is_running', return_value=True
        ), mock.patch.object(sniffer, '_drive_playback_once', return_value=None):
            sniffer._start_playback_drive()
            thread = sniffer._drive_thread
            self.assertIsNotNone(thread)

            sniffer.stop()

        self.assertIsNone(sniffer._drive_thread)
        self.assertIsNotNone(thread)
        assert thread is not None
        self.assertFalse(thread.is_alive(), 'stop() 後も自動操作のスレッドが残っている')


if __name__ == '__main__':
    unittest.main()
