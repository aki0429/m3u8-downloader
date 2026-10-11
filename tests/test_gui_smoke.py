"""GUI (m3u8dl/gui.py) と自己テストの動作検証。

手動クリックを伴わずに以下を確認する。

1. gui モジュールが構文・import ともに問題なく読み込めること。
2. run_selftest() が JSON を書き出して 0 を返し、必須キーを含むこと。
3. GUI をサブプロセスで起動し、数秒後に生存したままであること (起動スモークテスト)。
   兄弟モジュール (downloader / browser / cookies) が未作成でも起動が落ちないことを見る。
"""

import ast
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from m3u8dl import gui  # noqa: E402

# 起動スモークテストでプロセスを生存確認するまでの待機秒数
_STARTUP_WAIT_SECONDS = 5.0


class TestGuiSyntax(unittest.TestCase):
    """gui.py の構文と import を確認する。"""

    def test_source_parses_and_has_entrypoints(self) -> None:
        """ソースが ast で解析でき、契約の公開関数を持つこと。"""
        source = (REPOSITORY_ROOT / 'm3u8dl' / 'gui.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        function_names = {
            node.name for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.assertIn('main', function_names)
        self.assertIn('run_selftest', function_names)

    def test_module_imports_without_brother_modules(self) -> None:
        """兄弟モジュールが未作成でも import が成功し、欠落が記録されること。"""
        # 契約上の公開名がモジュールに存在すること
        self.assertTrue(hasattr(gui, 'main'))
        self.assertTrue(hasattr(gui, 'run_selftest'))
        # 欠落モジュールの記録はリストであること (未作成のものが入っていてよい)
        self.assertIsInstance(gui._MISSING_MODULES, list)


class TestRunSelftest(unittest.TestCase):
    """run_selftest() の出力を確認する。"""

    def test_selftest_writes_report_and_returns_zero(self) -> None:
        """自己テストが JSON を書き出して 0 を返し、必須キーを含むこと。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / 'selftest.json'
            exit_code = gui.run_selftest(report_path)

            self.assertEqual(exit_code, 0)
            self.assertTrue(report_path.exists(), '自己テストの JSON が書き出されていません')

            report = json.loads(report_path.read_text(encoding='utf-8'))

        # 必須キーの存在確認
        for key in (
            'version',
            'tkinter',
            'tk_usable',
            'ffmpeg_path',
            'aes_backend',
            'hls_selftest',
            'cookies_selftest',
            'missing_modules',
            'ok',
        ):
            self.assertIn(key, report)

        # コア検証 (tkinter / hls) が通っていること
        self.assertTrue(report['tkinter'])
        self.assertTrue(report['hls_selftest']['ok'])
        self.assertTrue(report['ok'])
        # AES バックエンド名が記録されていること
        self.assertTrue(report['aes_backend'])


class TestGuiStartupSmoke(unittest.TestCase):
    """GUI をサブプロセスで起動して生存を確認するスモークテスト。"""

    def test_gui_process_stays_alive(self) -> None:
        """GUI が起動直後に異常終了せず、数秒間生存し続けること。"""
        main_py = REPOSITORY_ROOT / 'main.py'
        if main_py.exists():
            command = [sys.executable, str(main_py)]
        else:
            # main.py が無い場合はモジュール実行で代替する
            command = [sys.executable, '-m', 'm3u8dl.gui']

        # 起動ログ (クラッシュ時の原因確認用) を一時ファイルへ逃がす
        # 子プロセスがログのハンドルを掴んだまま削除に失敗することがあるため、後始末の失敗は無視する
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            log_path = Path(temp_dir) / 'gui_smoke.log'
            with open(log_path, 'w', encoding='utf-8') as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=str(REPOSITORY_ROOT),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                try:
                    # 起動と描画が終わるまで待ってから生存確認する
                    time.sleep(_STARTUP_WAIT_SECONDS)
                    return_code = process.poll()
                    self.assertIsNone(
                        return_code,
                        f'GUI プロセスが起動後に終了しました (exit={return_code})',
                    )
                finally:
                    # 生存していれば terminate して確実に片付ける
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    # Windows がハンドルを解放するまでわずかに待つ (直後に一時ディレクトリを削除するため)
                    time.sleep(0.5)

            # クラッシュしていた場合に備えてログを失敗メッセージに残す
            log_text = log_path.read_text(encoding='utf-8', errors='replace').strip()
            if log_text:
                # 正常終了でも起動時のログが出ていることがあるため情報として添える
                self.assertNotIn('Traceback (most recent call last)', log_text)


if __name__ == '__main__':
    unittest.main()
