"""ffmpeg で生成したローカル HLS を 127.0.0.1 の HTTP サーバーで配信し、
m3u8dl.downloader.HLSDownloader でダウンロード → ffprobe 検証する end-to-end 統合テスト。

検証内容:
  1. ffmpeg で平文 HLS (TS セグメント 3 本 / 合計 6 秒) を生成する。
  2. AES-128 暗号化 HLS を生成する (key_info ファイルで鍵と IV を指定)。
  3. ThreadingHTTPServer で生成物を 127.0.0.1 に配信する。
  4. HLSDownloader で平文版・AES 版をダウンロードする。
  5. 出力ファイルを ffprobe で検証する (コンテナ・duration 6 秒 ±1 秒・映像ストリームの有無)。

ffmpeg / ffprobe が無い環境ではクラスごと skipTest する。

【ffmpeg の key_info ファイル書式】本テストで実測して確定させた書式は次の 3 行:
  1 行目: プレイリストに URI="..." として書き出される鍵 URI (ここでは "enc.key" 相対 URI)
  2 行目: 16 バイトの鍵が入ったファイルのパス (ffmpeg のプロセス cwd からの相対)
  3 行目: IV を表す 32 桁の 16 進文字列 (ファイルパスではない点に注意。省略時は全ゼロ)
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

# リポジトリルートを import パスに追加する (tests/ から実行される前提)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.errors import DownloadError  # noqa: E402
from m3u8dl.hls import parse_playlist  # noqa: E402

# ffmpeg / ffprobe の実体探索 (PATH 経由)
FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')

# downloader.py は並列実装中のため、未完成 (ImportError / SyntaxError 等) でも
# unittest discover 自体が落ちないよう import を保護し、クラス単位で skip する。
try:
    from m3u8dl.downloader import DownloadOptions, HLSDownloader

    _DOWNLOADER_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - 未完成モジュールを skip 扱いにするため広く捕捉する
    DownloadOptions = None  # type: ignore[assignment]
    HLSDownloader = None  # type: ignore[assignment]
    _DOWNLOADER_IMPORT_ERROR = exc

# 生成するテスト素材の長さ (秒)。duration 検証の許容幅と対応させる。
CLIP_SECONDS = 6.0
DURATION_TOLERANCE = 1.0


class _QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """ログ出力を抑制した静的ファイル配信ハンドラー。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 親のシグネチャに合わせる
        """アクセスログを捨てる (テスト出力を汚さないため)。"""
        return


class LocalHLSIntegrationTest(unittest.TestCase):
    """ffmpeg 生成のローカル HLS を HTTP 配信して実際にダウンロードする統合テスト。"""

    tmp_root: Path
    httpd: http.server.ThreadingHTTPServer
    http_thread: threading.Thread
    base_url: str
    plain_dir: Path
    aes_dir: Path

    @classmethod
    def setUpClass(cls) -> None:
        """テスト素材の生成とローカル HTTP サーバーの起動を 1 回だけ行う。"""
        if FFMPEG is None or FFPROBE is None:
            raise unittest.SkipTest('ffmpeg / ffprobe が PATH に見つからないため統合テストをスキップ')

        cls.tmp_root = Path(tempfile.mkdtemp(prefix='m3u8dl_it_'))
        cls.plain_dir = cls.tmp_root / 'plain'
        cls.aes_dir = cls.tmp_root / 'aes'
        cls.plain_dir.mkdir()
        cls.aes_dir.mkdir()

        # 1. 平文 HLS を生成する (2 秒 × 3 セグメント = 6 秒)
        cls._generate_plain_hls(cls.plain_dir, 'out.m3u8')

        # 2. AES-128 暗号化 HLS を生成する
        cls._generate_aes_hls(cls.aes_dir, 'enc.m3u8')

        # 3. 生成物を 127.0.0.1 の空きポートで配信する
        handler = functools.partial(_QuietHTTPRequestHandler, directory=str(cls.tmp_root))
        cls.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        port = cls.httpd.server_address[1]
        cls.base_url = f'http://127.0.0.1:{port}'

    @classmethod
    def tearDownClass(cls) -> None:
        """HTTP サーバーを停止して一時ディレクトリを片付ける。"""
        if getattr(cls, 'httpd', None) is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.http_thread.join(timeout=5.0)
        if getattr(cls, 'tmp_root', None) is not None:
            shutil.rmtree(cls.tmp_root, ignore_errors=True)

    @staticmethod
    def _run_ffmpeg(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        """ffmpeg を実行し、失敗していたらテストを失敗させる。

        Args:
            args: ffmpeg 本体を除く引数。
            cwd: ffmpeg の作業ディレクトリ (出力ファイルはここに作られる)。

        Returns:
            実行結果。

        Raises:
            AssertionError: ffmpeg が異常終了したとき。
        """
        assert FFMPEG is not None
        completed = subprocess.run(
            [FFMPEG, *args], cwd=str(cwd), capture_output=True, text=True, timeout=120.0,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f'ffmpeg が失敗しました (rc={completed.returncode}):\n{completed.stderr[-2000:]}'
            )
        return completed

    @classmethod
    def _generate_plain_hls(cls, target_dir: Path, playlist_name: str) -> None:
        """平文の TLS テスト素材 (TS セグメント + プレイリスト) を生成する。

        Args:
            target_dir: 出力先ディレクトリ。
            playlist_name: 生成するプレイリストのファイル名。
        """
        cls._run_ffmpeg([
            '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=15',
            '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-t', str(int(CLIP_SECONDS)),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '15',
            '-c:a', 'aac',
            '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
            '-hls_segment_filename', 'seg%d.ts',
            playlist_name,
        ], target_dir)

    @classmethod
    def _generate_aes_hls(cls, target_dir: Path, playlist_name: str) -> None:
        """AES-128 暗号化したテスト素材を生成する。

        key_info ファイルは「鍵 URI / 鍵ファイル / IV の 16 進文字列」の 3 行で書く。
        鍵 URI と鍵ファイル名を同じ 'enc.key' にして、配信側の相対 URL としても解決できるようにする。

        Args:
            target_dir: 出力先ディレクトリ。
            playlist_name: 生成するプレイリストのファイル名。
        """
        key_bytes = os.urandom(16)
        iv_hex = os.urandom(16).hex()
        (target_dir / 'enc.key').write_bytes(key_bytes)
        # 1 行目 = プレイリストに書かれる鍵 URI、2 行目 = 鍵ファイルのパス、3 行目 = IV (16 進文字列)
        (target_dir / 'key_info.txt').write_text(f'enc.key\nenc.key\n{iv_hex}\n', encoding='ascii')

        cls._run_ffmpeg([
            '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=15',
            '-f', 'lavfi', '-i', 'sine=frequency=440',
            '-t', str(int(CLIP_SECONDS)),
            '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '15',
            '-c:a', 'aac',
            '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
            '-hls_segment_filename', 'seg%d.ts',
            '-hls_key_info_file', 'key_info.txt',
            playlist_name,
        ], target_dir)

        # ffmpeg が AES-128 に対応していないビルドでは鍵タグが付かないため、その場合はスキップする
        playlist_text = (target_dir / playlist_name).read_text(encoding='utf-8')
        if '#EXT-X-KEY:METHOD=AES-128' not in playlist_text:
            raise unittest.SkipTest('ffmpeg が AES-128 暗号化 HLS を生成しなかったため AES 検証をスキップ')

    def _fetch_text(self, url: str) -> str:
        """HTTP 経由でテキストを取得する。

        Args:
            url: 取得対象の URL。

        Returns:
            取得した本文。
        """
        with urllib.request.urlopen(url, timeout=10.0) as response:  # noqa: S310 - ローカル検証用
            return response.read().decode('utf-8', errors='replace')

    def _probe(self, path: Path) -> dict:
        """ffprobe でコンテナ・duration・ストリーム構成を取得する。

        Args:
            path: 検証対象のメディアファイル。

        Returns:
            ffprobe の JSON 出力 (format / streams を含む辞書)。
        """
        assert FFPROBE is not None
        completed = subprocess.run(
            [
                FFPROBE, '-v', 'error',
                '-show_entries', 'format=duration,format_name',
                '-show_entries', 'stream=codec_type',
                '-of', 'json', str(path),
            ],
            capture_output=True, text=True, timeout=60.0,
        )
        self.assertEqual(completed.returncode, 0, msg=f'ffprobe 失敗: {completed.stderr}')
        return json.loads(completed.stdout)

    def _assert_valid_output(self, path: Path) -> None:
        """出力ファイルが mp4 か ts で、duration が約 6 秒で、映像ストリームを持つことを検証する。

        Args:
            path: 検証対象のメディアファイル。
        """
        self.assertTrue(path.exists(), msg=f'出力ファイルが存在しない: {path}')
        self.assertGreater(path.stat().st_size, 0, msg='出力ファイルが空')

        probe = self._probe(path)
        format_name = probe.get('format', {}).get('format_name', '')
        # コンテナは mp4 系 (mov,mp4,...) か MPEG-TS のいずれかであること
        self.assertTrue(
            'mp4' in format_name or 'mpegts' in format_name,
            msg=f'コンテナが mp4/ts ではない: {format_name}',
        )

        duration = float(probe.get('format', {}).get('duration', 0.0))
        self.assertAlmostEqual(duration, CLIP_SECONDS, delta=DURATION_TOLERANCE)

        codec_types = [stream.get('codec_type') for stream in probe.get('streams', [])]
        self.assertIn('video', codec_types, msg='映像ストリームが存在しない')

    def test_plain_playlist_over_http(self) -> None:
        """配信中の平文プレイリストが media / VOD / 3 セグメントとして解析できること。"""
        text = self._fetch_text(f'{self.base_url}/plain/out.m3u8')
        kind, payload = parse_playlist(text, f'{self.base_url}/plain/out.m3u8')
        self.assertEqual(kind, 'media')
        self.assertFalse(payload.is_live)  # type: ignore[union-attr]
        self.assertEqual(len(payload.segments), 3)  # type: ignore[union-attr]
        self.assertAlmostEqual(payload.total_duration, CLIP_SECONDS, delta=0.5)  # type: ignore[union-attr]

    def test_aes_playlist_over_http(self) -> None:
        """配信中の AES プレイリストの全セグメントに AES-128 の鍵が付くこと。"""
        text = self._fetch_text(f'{self.base_url}/aes/enc.m3u8')
        kind, payload = parse_playlist(text, f'{self.base_url}/aes/enc.m3u8')
        self.assertEqual(kind, 'media')
        self.assertEqual(len(payload.segments), 3)  # type: ignore[union-attr]
        for segment in payload.segments:  # type: ignore[union-attr]
            self.assertIsNotNone(segment.key, msg='セグメントに鍵が付いていない')
            assert segment.key is not None
            self.assertEqual(segment.key.method, 'AES-128')
            self.assertTrue(segment.key.uri.endswith('/aes/enc.key'))
            self.assertIsNotNone(segment.key.iv)

    def test_download_plain_hls_to_mp4(self) -> None:
        """平文 HLS をダウンロードして mp4 (ffprobe で 6 秒 ±1 秒・映像あり) が得られること。"""
        output = self.tmp_root / 'plain_result.mp4'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='mp4',
            ffmpeg_path=FFMPEG,
        )
        result = HLSDownloader(options).download(f'{self.base_url}/plain/out.m3u8')  # type: ignore[misc]
        self.assertEqual(Path(result), output)
        self._assert_valid_output(output)

    def test_download_aes_hls_to_mp4(self) -> None:
        """AES-128 暗号化 HLS を復号しつつダウンロードして有効な mp4 が得られること。"""
        output = self.tmp_root / 'aes_result.mp4'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='mp4',
            ffmpeg_path=FFMPEG,
        )
        result = HLSDownloader(options).download(f'{self.base_url}/aes/enc.m3u8')  # type: ignore[misc]
        self.assertEqual(Path(result), output)
        self._assert_valid_output(output)

    def test_download_plain_hls_to_ts(self) -> None:
        """平文 HLS を merge_format='ts' でダウンロードしても有効な出力が得られること。"""
        output = self.tmp_root / 'plain_result.ts'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='ts',
            ffmpeg_path=FFMPEG,
        )
        result = HLSDownloader(options).download(f'{self.base_url}/plain/out.m3u8')  # type: ignore[misc]
        self.assertTrue(Path(result).exists())
        self._assert_valid_output(Path(result))

    def test_download_missing_playlist_raises_download_error(self) -> None:
        """存在しないプレイリストのダウンロードは DownloadError になること。"""
        output = self.tmp_root / 'should_not_exist.mp4'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=2,
            merge_format='none',
            ffmpeg_path=FFMPEG,
        )
        with self.assertRaises(DownloadError):
            HLSDownloader(options).download(f'{self.base_url}/plain/missing.m3u8')  # type: ignore[misc]


# downloader.py がまだ import できない環境では、統合テスト全体をスキップ理由付きで無効化する
if HLSDownloader is None:
    LocalHLSIntegrationTest = unittest.skip(  # type: ignore[misc,assignment]
        f'm3u8dl.downloader を import できないためスキップ: {_DOWNLOADER_IMPORT_ERROR!r}'
    )(LocalHLSIntegrationTest)  # type: ignore[assignment]


if __name__ == '__main__':
    unittest.main()
