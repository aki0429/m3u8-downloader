"""ffmpeg で生成した fMP4 (EXT-X-MAP あり) の HLS を配信し、実際にダウンロードして検証する統合テスト。

TS セグメントの HLS は `test_integration_local_hls.py` で検証しているが、fMP4 (init.mp4 + .m4s) は
結合の扱いがまったく異なるため、実測で押さえておく。

検証内容:
  1. ffmpeg で fMP4 HLS (init.mp4 + .m4s 3 本 / 合計 6 秒) を生成する。
  2. ライブ扱いにするため #EXT-X-ENDLIST / #EXT-X-PLAYLIST-TYPE を除いたメディアプレイリストも作る。
  3. ThreadingHTTPServer で 127.0.0.1 に配信する。
  4. VOD (mp4 指定 / ts 指定) とライブ収録 (max_segments 指定) でダウンロードする。
  5. 出力を ffprobe で検証する。あわせて「初期化セグメントが先頭に来ていること」をバイト列で確認する。

fMP4 の各フラグメント (.m4s) は単体では解析できず、ffmpeg の concat デマルチプレクサでは結合できない
(no tfhd エラーになる)。初期化セグメント + フラグメントのバイナリ結合が正しい結合方法である。
"""

from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.hls import parse_playlist  # noqa: E402

FFMPEG = shutil.which('ffmpeg')
FFPROBE = shutil.which('ffprobe')

try:
    from m3u8dl.downloader import DownloadOptions, HLSDownloader

    _DOWNLOADER_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # noqa: BLE001 - 未完成モジュールを skip 扱いにするため広く捕捉する
    DownloadOptions = None  # type: ignore[assignment]
    HLSDownloader = None  # type: ignore[assignment]
    _DOWNLOADER_IMPORT_ERROR = exc

# 生成するテスト素材の長さ (秒)
CLIP_SECONDS = 6.0
DURATION_TOLERANCE = 1.0


class _QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """ログ出力を抑制した静的ファイル配信ハンドラー。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 親のシグネチャに合わせる
        """アクセスログを捨てる (テスト出力を汚さないため)。"""
        return


class LocalFMP4IntegrationTest(unittest.TestCase):
    """fMP4 HLS を HTTP 配信して実際にダウンロードする統合テスト。"""

    tmp_root: Path
    media_dir: Path
    httpd: http.server.ThreadingHTTPServer
    http_thread: threading.Thread
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """テスト素材の生成とローカル HTTP サーバーの起動を 1 回だけ行う。"""
        if FFMPEG is None or FFPROBE is None:
            raise unittest.SkipTest('ffmpeg / ffprobe が PATH に見つからないため統合テストをスキップ')

        cls.tmp_root = Path(tempfile.mkdtemp(prefix='m3u8dl_fmp4_'))
        cls.media_dir = cls.tmp_root / 'fmp4'
        cls.media_dir.mkdir()

        cls._generate_fmp4_hls(cls.media_dir, 'out.m3u8')

        # ライブ扱いのプレイリストを作る (ENDLIST と VOD 指定を外す)
        playlist_text = (cls.media_dir / 'out.m3u8').read_text(encoding='utf-8')
        live_text = '\n'.join(
            line for line in playlist_text.splitlines()
            if not line.startswith('#EXT-X-ENDLIST') and not line.startswith('#EXT-X-PLAYLIST-TYPE')
        ) + '\n'
        (cls.media_dir / 'live.m3u8').write_text(live_text, encoding='utf-8')

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
            shutil.rmtree(cls.tmp_root, ignore_errors=True)

    @classmethod
    def _generate_fmp4_hls(cls, target_dir: Path, playlist_name: str) -> None:
        """fMP4 (init.mp4 + .m4s) のテスト素材を生成する。

        Args:
            target_dir: 出力先ディレクトリ。
            playlist_name: 生成するプレイリストのファイル名。

        Raises:
            AssertionError: ffmpeg が失敗したとき。
            unittest.SkipTest: ffmpeg が fMP4 の HLS を生成できなかったとき。
        """
        assert FFMPEG is not None
        completed = subprocess.run(
            [
                FFMPEG, '-y',
                '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=15',
                '-f', 'lavfi', '-i', 'sine=frequency=440',
                '-t', str(int(CLIP_SECONDS)),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '15',
                '-c:a', 'aac',
                '-f', 'hls', '-hls_time', '2', '-hls_list_size', '0',
                '-hls_segment_type', 'fmp4',
                '-hls_fmp4_init_filename', 'init.mp4',
                '-hls_segment_filename', 'seg%d.m4s',
                playlist_name,
            ],
            cwd=str(target_dir), capture_output=True, text=True, timeout=120.0,
        )
        if completed.returncode != 0:
            raise AssertionError(f'ffmpeg が失敗しました (rc={completed.returncode}):\n{completed.stderr[-2000:]}')
        text = (target_dir / playlist_name).read_text(encoding='utf-8')
        if '#EXT-X-MAP:' not in text:
            raise unittest.SkipTest('ffmpeg が fMP4 (EXT-X-MAP) の HLS を生成しなかったためスキップ')

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
            ffprobe の JSON 出力。
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

    def _assert_valid_fmp4_output(self, path: Path) -> None:
        """出力が有効な mp4 で、先頭に初期化セグメントが入っていることを検証する。

        Args:
            path: 検証対象のメディアファイル。
        """
        self.assertTrue(path.exists(), msg=f'出力ファイルが存在しない: {path}')
        self.assertGreater(path.stat().st_size, 0, msg='出力ファイルが空')

        probe = self._probe(path)
        format_name = probe.get('format', {}).get('format_name', '')
        self.assertIn('mp4', format_name, msg=f'コンテナが mp4 ではない: {format_name}')
        codec_types = [stream.get('codec_type') for stream in probe.get('streams', [])]
        self.assertIn('video', codec_types, msg='映像ストリームが存在しない')

        # 初期化セグメント (moov/moof の前提) が先頭に来ていること
        init_bytes = (self.media_dir / 'init.mp4').read_bytes()
        with path.open('rb') as stream:
            head = stream.read(len(init_bytes))
        self.assertEqual(head, init_bytes, msg='初期化セグメントが先頭に結合されていない')

    def test_playlist_reports_init_segment(self) -> None:
        """配信中の fMP4 プレイリストが初期化セグメント付きとして解析できること。"""
        text = self._fetch_text(f'{self.base_url}/fmp4/out.m3u8')
        kind, payload = parse_playlist(text, f'{self.base_url}/fmp4/out.m3u8')
        self.assertEqual(kind, 'media')
        self.assertTrue(payload.has_map)  # type: ignore[union-attr]
        self.assertIsNotNone(payload.init_url)  # type: ignore[union-attr]

    def test_download_fmp4_to_mp4(self) -> None:
        """fMP4 を mp4 指定でダウンロードして有効な mp4 (約 6 秒) が得られること。"""
        output = self.tmp_root / 'fmp4_result.mp4'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='mp4',
            ffmpeg_path=FFMPEG,
        )
        result = Path(HLSDownloader(options).download(f'{self.base_url}/fmp4/out.m3u8'))  # type: ignore[misc]
        self.assertEqual(result, output)
        self._assert_valid_fmp4_output(output)
        duration = float(self._probe(output).get('format', {}).get('duration', 0.0))
        self.assertAlmostEqual(duration, CLIP_SECONDS, delta=DURATION_TOLERANCE)

    def test_download_fmp4_with_ts_format_saves_mp4(self) -> None:
        """fMP4 は ts 指定でも mp4 として保存され、有効な mp4 になること。"""
        output = self.tmp_root / 'fmp4_as_ts.ts'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='ts',
            ffmpeg_path=FFMPEG,
        )
        result = Path(HLSDownloader(options).download(f'{self.base_url}/fmp4/out.m3u8'))  # type: ignore[misc]
        # 中身は mp4 なので、拡張子も .mp4 に直される
        self.assertEqual(result.suffix, '.mp4', msg=f'拡張子が mp4 ではない: {result}')
        self._assert_valid_fmp4_output(result)

    def test_download_fmp4_live_records_init_segment(self) -> None:
        """ENDLIST の無い fMP4 をライブ収録しても、初期化セグメント込みの有効な mp4 になること。"""
        output = self.tmp_root / 'fmp4_live.mp4'
        options = DownloadOptions(
            output_path=output,
            headers={},
            concurrency=4,
            merge_format='mp4',
            ffmpeg_path=FFMPEG,
            live=True,
            max_segments=2,
        )
        result = Path(HLSDownloader(options).download(f'{self.base_url}/fmp4/live.m3u8'))  # type: ignore[misc]
        self._assert_valid_fmp4_output(result)


if HLSDownloader is None:
    LocalFMP4IntegrationTest = unittest.skip(  # type: ignore[misc,assignment]
        f'm3u8dl.downloader を import できないためスキップ: {_DOWNLOADER_IMPORT_ERROR!r}'
    )(LocalFMP4IntegrationTest)  # type: ignore[assignment]


if __name__ == '__main__':
    unittest.main()
