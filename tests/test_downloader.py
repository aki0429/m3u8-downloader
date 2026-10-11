"""HLSDownloader の実測テスト。

127.0.0.1 の空きポートで ThreadingHTTPServer を起動し、並列取得・リトライ・
AES-128 復号・ByteRange・キャンセル・max_segments・ライブ収録を検証する。
ffmpeg を使う実際の結合は実 TS が必要なため別担当の領域とし、ここでは
ffmpeg 不在時のバイナリ結合フォールバックのみを確認する。
"""

import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.downloader import (  # noqa: E402
    DownloadOptions,
    DownloadProgress,
    HLSDownloader,
)
from m3u8dl.errors import DownloadCancelled, DownloadError, PlaylistError  # noqa: E402
from m3u8dl.hls import Variant  # noqa: E402

# テスト内でのみ pycryptodome を使って暗号化データを生成する
try:
    from Crypto.Cipher import AES as _PYCRYPTODOME_AES

    _HAS_AES = True
except ImportError:  # pragma: no cover - 環境依存
    _PYCRYPTODOME_AES = None
    _HAS_AES = False


# ----------------------------------------------------------------------
# テスト用 HTTP サーバー
# ----------------------------------------------------------------------


def _send_bytes(handler: BaseHTTPRequestHandler, status: int, body: bytes) -> None:
    """ボディを Content-Length 付きで送信する。

    Args:
        handler: 送信を行うリクエストハンドラ。
        status: HTTP ステータスコード。
        body: 送信するバイト列。
    """
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/octet-stream')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _static(body: bytes) -> Callable[[BaseHTTPRequestHandler], None]:
    """常に同じバイト列を 200 で返すルートを作る。"""

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        _send_bytes(handler_self, 200, body)

    return handler


def _text(body: str) -> Callable[[BaseHTTPRequestHandler], None]:
    """常に同じテキストを 200 で返すルートを作る。"""
    return _static(body.encode('utf-8'))


def _fail_then_ok(body: bytes, fail_count: int) -> Callable[[BaseHTTPRequestHandler], None]:
    """最初の fail_count 回は 500 を返し、その後は成功するルートを作る。"""
    state = {'count': 0}

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        state['count'] += 1
        if state['count'] <= fail_count:
            _send_bytes(handler_self, 500, b'')
            return
        _send_bytes(handler_self, 200, body)

    return handler


def _always_fail(status: int) -> Callable[[BaseHTTPRequestHandler], None]:
    """常に指定ステータスで失敗するルートを作る。"""

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        _send_bytes(handler_self, status, b'')

    return handler


def _delayed(body: bytes, delay: float) -> Callable[[BaseHTTPRequestHandler], None]:
    """delay 秒待ってから本文を返すルートを作る (キャンセル検証用)。"""

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        time.sleep(delay)
        _send_bytes(handler_self, 200, body)

    return handler


def _range_file(
    body: bytes,
    recorded: list[str | None] | None = None,
) -> Callable[[BaseHTTPRequestHandler], None]:
    """Range ヘッダに応じて部分コンテンツ (206) を返すルートを作る。"""

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        range_header = handler_self.headers.get('Range')
        if recorded is not None:
            recorded.append(range_header)
        if range_header and range_header.startswith('bytes='):
            start_text, _, end_text = range_header[len('bytes='):].partition('-')
            start = int(start_text)
            end = int(end_text)
            chunk = body[start:end + 1]
            handler_self.send_response(206)
            handler_self.send_header('Content-Type', 'application/octet-stream')
            handler_self.send_header('Content-Range', f'bytes {start}-{end}/{len(body)}')
            handler_self.send_header('Content-Length', str(len(chunk)))
            handler_self.end_headers()
            handler_self.wfile.write(chunk)
        else:
            _send_bytes(handler_self, 200, body)

    return handler


def _dynamic(provider: Callable[[], str]) -> Callable[[BaseHTTPRequestHandler], None]:
    """呼び出しのたびに内容が変わるプレイリストを返すルートを作る。"""

    def handler(handler_self: BaseHTTPRequestHandler) -> None:
        _send_bytes(handler_self, 200, provider().encode('utf-8'))

    return handler


class _Handler(BaseHTTPRequestHandler):
    """テスト用のリクエストハンドラ。経路ごとのルート関数に処理を委譲する。"""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - 標準の引数名
        # テスト出力を汚さないようにアクセスログを抑制する
        pass

    def do_GET(self) -> None:
        app: _TestServer = self.server.app  # type: ignore[attr-defined]
        path = urllib.parse.urlsplit(self.path).path
        with app.lock:
            app.requests[path] = app.requests.get(path, 0) + 1
            app.headers.append(dict(self.headers.items()))
        route = app.routes.get(path)
        if route is None:
            _send_bytes(self, 404, b'')
            return
        route(self)


class _TestServer:
    """テスト用に 127.0.0.1 の空きポートで起動する HTTP サーバー。"""

    def __init__(self) -> None:
        """サーバーを起動する。routes にパス -> ハンドラを登録して使う。"""
        self.routes: dict[str, Callable[[BaseHTTPRequestHandler], None]] = {}
        self.requests: dict[str, int] = {}
        self.headers: list[dict[str, str]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.server.app = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        """リッスン中のポート番号を返す。"""
        return int(self.server.server_address[1])

    def url(self, path: str) -> str:
        """パスから絶対 URL を組み立てる。"""
        return f'http://127.0.0.1:{self.port}{path}'

    def request_count(self, path: str) -> int:
        """指定パスへのリクエスト回数を返す。"""
        with self.lock:
            return self.requests.get(path, 0)

    def close(self) -> None:
        """サーバーを停止する。"""
        self.server.shutdown()
        self.server.server_close()


# ----------------------------------------------------------------------
# プレイリスト生成ヘルパー
# ----------------------------------------------------------------------


def _media_playlist(
    segment_urls: list[str],
    media_sequence: int | None = None,
    endlist: bool = True,
    key_line: str | None = None,
    byte_ranges: list[tuple[int, int]] | None = None,
) -> str:
    """テスト用のメディアプレイリストを組み立てる。

    Args:
        segment_urls: セグメント URL の並び。
        media_sequence: #EXT-X-MEDIA-SEQUENCE の値。None なら出力しない。
        endlist: True なら #EXT-X-ENDLIST を付ける。
        key_line: #EXT-X-KEY 行。None なら出力しない。
        byte_ranges: 各セグメントの (length, offset)。None なら #EXT-X-BYTERANGE を出さない。

    Returns:
        プレイリスト本文。
    """
    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '#EXT-X-TARGETDURATION:4']
    if media_sequence is not None:
        lines.append(f'#EXT-X-MEDIA-SEQUENCE:{media_sequence}')
    if key_line is not None:
        lines.append(key_line)
    for index, url in enumerate(segment_urls):
        lines.append('#EXTINF:4.0,')
        if byte_ranges is not None:
            length, offset = byte_ranges[index]
            lines.append(f'#EXT-X-BYTERANGE:{length}@{offset}')
        lines.append(url)
    if endlist:
        lines.append('#EXT-X-ENDLIST')
    return '\n'.join(lines) + '\n'


def _key_line(uri: str = 'key.bin', iv_hex: str | None = None) -> str:
    """#EXT-X-KEY 行を組み立てる。"""
    attributes = f'METHOD=AES-128,URI="{uri}"'
    if iv_hex is not None:
        attributes += f',IV={iv_hex}'
    return f'#EXT-X-KEY:{attributes}'


def _pkcs7_pad(data: bytes) -> bytes:
    """PKCS#7 パディングを付与する。"""
    pad = 16 - (len(data) % 16)
    return data + bytes([pad]) * pad


def _encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """pycryptodome で AES-128-CBC 暗号化する (テストデータ生成用)。"""
    assert _PYCRYPTODOME_AES is not None
    return _PYCRYPTODOME_AES.new(key, _PYCRYPTODOME_AES.MODE_CBC, iv).encrypt(_pkcs7_pad(plaintext))


# ----------------------------------------------------------------------
# テスト本体
# ----------------------------------------------------------------------


class TestSegmentDownload(unittest.TestCase):
    """セグメントの並列取得と結合を検証する。"""

    def setUp(self) -> None:
        """サーバーと一時ディレクトリを用意する。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _TestServer()
        self.addCleanup(self.server.close)
        self.tmp_path = Path(self.tmp.name)

    def test_parallel_download_and_binary_merge(self) -> None:
        """concurrency 2 で取得し、インデックス順にバイナリ結合されること。"""
        payloads = [bytes([65 + index]) * (1000 + index) for index in range(4)]
        for index, payload in enumerate(payloads):
            self.server.routes[f'/seg{index}.ts'] = _static(payload)
        self.server.routes['/media.m3u8'] = _text(_media_playlist([f'/seg{index}.ts' for index in range(4)]))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=2,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        data = result.read_bytes()
        self.assertEqual(len(data), sum(len(payload) for payload in payloads))
        # 並列取得でも並び順が崩れないこと (各セグメントは異なるバイトで構成)
        self.assertEqual(data, b''.join(payloads))
        self.assertEqual(result.suffix, '.ts')

    def test_retry_succeeds_after_transient_errors(self) -> None:
        """最初の 2 回 500 を返すセグメントがリトライで成功すること。"""
        good = b'G' * 512
        self.server.routes['/seg0.ts'] = _static(b'A' * 128)
        self.server.routes['/seg1.ts'] = _fail_then_ok(good, fail_count=2)
        self.server.routes['/media.m3u8'] = _text(_media_playlist(['/seg0.ts', '/seg1.ts']))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=1,
            retries=3,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.read_bytes(), b'A' * 128 + good)
        # 2 回失敗 + 1 回成功 = 3 回アクセスされること
        self.assertEqual(self.server.request_count('/seg1.ts'), 3)

    def test_retry_exhausted_raises_download_error(self) -> None:
        """リトライしても回復しない場合は DownloadError になること。"""
        self.server.routes['/seg0.ts'] = _always_fail(500)
        self.server.routes['/media.m3u8'] = _text(_media_playlist(['/seg0.ts']))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=1,
            retries=2,
            merge_format='none',
        )
        with self.assertRaises(DownloadError) as context:
            HLSDownloader(options).download(self.server.url('/media.m3u8'))
        # エラーメッセージに URL と HTTP ステータスが含まれること
        self.assertIn('/seg0.ts', str(context.exception))
        self.assertIn('500', str(context.exception))

    def test_max_segments_limits_download(self) -> None:
        """max_segments で先頭 N 件だけ取得し、残りは取得しないこと。"""
        payloads = [bytes([100 + index]) * 200 for index in range(4)]
        for index, payload in enumerate(payloads):
            self.server.routes[f'/seg{index}.ts'] = _static(payload)
        self.server.routes['/media.m3u8'] = _text(_media_playlist([f'/seg{index}.ts' for index in range(4)]))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=2,
            max_segments=2,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.read_bytes(), payloads[0] + payloads[1])
        self.assertEqual(self.server.request_count('/seg2.ts'), 0)
        self.assertEqual(self.server.request_count('/seg3.ts'), 0)

    def test_headers_are_sent(self) -> None:
        """DownloadOptions.headers がそのまま送信されること。"""
        self.server.routes['/seg0.ts'] = _static(b'Z' * 64)
        self.server.routes['/media.m3u8'] = _text(_media_playlist(['/seg0.ts']))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            headers={'X-Test-Header': 'hello', 'Referer': 'https://example.com/'},
            merge_format='none',
        )
        HLSDownloader(options).download(self.server.url('/media.m3u8'))

        with self.server.lock:
            sent = list(self.server.headers)
        self.assertTrue(any(header.get('X-Test-Header') == 'hello' for header in sent))
        self.assertTrue(any(header.get('Referer') == 'https://example.com/' for header in sent))

    def test_keep_segments_preserves_temp_files(self) -> None:
        """keep_segments=True なら一時セグメントが残ること。"""
        self.server.routes['/seg0.ts'] = _static(b'K' * 64)
        self.server.routes['/seg1.ts'] = _static(b'L' * 64)
        self.server.routes['/media.m3u8'] = _text(_media_playlist(['/seg0.ts', '/seg1.ts']))

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            merge_format='none',
            keep_segments=True,
        )
        HLSDownloader(options).download(self.server.url('/media.m3u8'))

        segment_dir = self.tmp_path / 'out_segments'
        self.assertTrue(segment_dir.exists())
        self.assertEqual(len(list(segment_dir.glob('seg_*.part'))), 2)


class TestByteRange(unittest.TestCase):
    """EXT-X-BYTERANGE 付きセグメントの部分取得を検証する。"""

    def setUp(self) -> None:
        """サーバーと一時ディレクトリを用意する。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _TestServer()
        self.addCleanup(self.server.close)
        self.tmp_path = Path(self.tmp.name)

    def test_byte_range_segments(self) -> None:
        """Range ヘッダで 1 ファイルの複数レンジを取得できること。"""
        content = bytes((index % 256) for index in range(800))
        recorded: list[str | None] = []
        self.server.routes['/data.bin'] = _range_file(content, recorded)
        ranges = [(200, 0), (200, 200), (200, 400), (200, 600)]
        self.server.routes['/media.m3u8'] = _text(
            _media_playlist(['/data.bin'] * 4, byte_ranges=ranges),
        )

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=2,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.read_bytes(), content)
        self.assertEqual(len(recorded), 4)
        # 並列取得のため到着順は不定。送信された Range の集合が一致することを見る
        self.assertEqual(
            sorted(recorded),
            sorted(['bytes=0-199', 'bytes=200-399', 'bytes=400-599', 'bytes=600-799']),
        )


class TestAESDecryption(unittest.TestCase):
    """AES-128 暗号化セグメントの復号を検証する。"""

    def setUp(self) -> None:
        """サーバーと一時ディレクトリを用意する。"""
        if not _HAS_AES:
            self.skipTest('pycryptodome が無いため AES テストをスキップ')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _TestServer()
        self.addCleanup(self.server.close)
        self.tmp_path = Path(self.tmp.name)
        self.key = bytes(range(16))

    def _register_encrypted(self, plaintexts: list[bytes], iv: bytes | None) -> None:
        """暗号化セグメントと鍵をサーバーに登録する。

        Args:
            plaintexts: 各セグメントの平文。
            iv: 使用する IV。None ならメディアシーケンス番号から生成した IV を使う。
        """
        self.server.routes['/key.bin'] = _static(self.key)
        for index, plaintext in enumerate(plaintexts):
            segment_iv = iv if iv is not None else index.to_bytes(16, 'big')
            self.server.routes[f'/enc{index}.ts'] = _static(_encrypt(self.key, segment_iv, plaintext))
        iv_hex = iv.hex() if iv is not None else None
        self.server.routes['/media.m3u8'] = _text(
            _media_playlist(
                [f'/enc{index}.ts' for index in range(len(plaintexts))],
                key_line=_key_line('key.bin', iv_hex),
            ),
        )

    def test_aes128_explicit_iv(self) -> None:
        """IV 指定ありの AES-128 セグメントが正しく復号されること。"""
        iv = bytes.fromhex('00112233445566778899aabbccddeeff')
        plaintexts = [bytes([index + 1]) * 128 for index in range(3)]
        self._register_encrypted(plaintexts, iv)

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=2,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.read_bytes(), b''.join(plaintexts))
        # 鍵は 1 回だけ取得されること (キャッシュが効いている)
        self.assertEqual(self.server.request_count('/key.bin'), 1)

    def test_aes128_derived_iv(self) -> None:
        """IV 未指定時にメディアシーケンス番号から IV が生成されること。"""
        # 平文長を 16 の倍数以外にしてパディング処理も通す
        plaintexts = [bytes([200 - index]) * (100 + index) for index in range(4)]
        self._register_encrypted(plaintexts, None)

        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            concurrency=2,
            merge_format='none',
        )
        result = HLSDownloader(options).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.read_bytes(), b''.join(plaintexts))

    def test_sample_aes_rejected(self) -> None:
        """SAMPLE-AES は DownloadError で早期に拒否されること。"""
        self.server.routes['/seg0.ts'] = _static(b'x' * 64)
        self.server.routes['/media.m3u8'] = _text(
            _media_playlist(['/seg0.ts'], key_line='#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"'),
        )
        options = DownloadOptions(output_path=self.tmp_path / 'out.mp4', merge_format='none')
        with self.assertRaises(DownloadError) as context:
            HLSDownloader(options).download(self.server.url('/media.m3u8'))
        self.assertIn('SAMPLE-AES', str(context.exception))


class TestCancellation(unittest.TestCase):
    """別スレッドからの cancel() を検証する。"""

    def test_cancel_raises_download_cancelled(self) -> None:
        """cancel() により DownloadCancelled が送出されること。"""
        with tempfile.TemporaryDirectory() as tmp:
            server = _TestServer()
            self.addCleanup(server.close)
            payload = b'X' * 4096
            for index in range(8):
                server.routes[f'/seg{index}.ts'] = _delayed(payload, 0.3)
            server.routes['/media.m3u8'] = _text(
                _media_playlist([f'/seg{index}.ts' for index in range(8)]),
            )

            options = DownloadOptions(
                output_path=Path(tmp) / 'out.mp4',
                concurrency=2,
                merge_format='none',
            )
            events: list[DownloadProgress] = []
            downloader = HLSDownloader(options, progress_callback=events.append)

            errors: list[BaseException] = []

            def run() -> None:
                try:
                    downloader.download(server.url('/media.m3u8'))
                except BaseException as error:  # noqa: BLE001 - テストで捕捉して検証する
                    errors.append(error)

            thread = threading.Thread(target=run)
            thread.start()

            # 最初のセグメント取得が完了する (=進捗が来る) まで待ってからキャンセルする
            deadline = time.monotonic() + 10.0
            while not any(event.phase == 'download' and event.downloaded_segments >= 1 for event in events):
                if time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            downloader.cancel()
            thread.join(timeout=15)

            self.assertFalse(thread.is_alive(), 'cancel() 後もスレッドが動き続けています')
            self.assertTrue(
                any(isinstance(error, DownloadCancelled) for error in errors),
                f'DownloadCancelled が送出されませんでした: {errors}',
            )


class TestVariantSelection(unittest.TestCase):
    """バリアント選択と master プレイリストのダウンロードを検証する。"""

    def setUp(self) -> None:
        """サーバーと一時ディレクトリを用意する。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _TestServer()
        self.addCleanup(self.server.close)
        self.tmp_path = Path(self.tmp.name)

    def test_select_variant_modes(self) -> None:
        """best / worst / index 指定の選択結果が正しいこと。"""
        variants = [
            Variant(url='low', bandwidth=300000, resolution='640x360'),
            Variant(url='high', bandwidth=2000000, resolution='1280x720'),
            Variant(url='mid', bandwidth=1000000, resolution='960x540'),
        ]
        self.assertEqual(HLSDownloader.select_variant(variants, 'best').url, 'high')
        self.assertEqual(HLSDownloader.select_variant(variants, 'worst').url, 'low')
        self.assertEqual(HLSDownloader.select_variant(variants, 'lowest').url, 'low')
        self.assertEqual(HLSDownloader.select_variant(variants, 'best', index=0).url, 'low')
        with self.assertRaises(PlaylistError):
            HLSDownloader.select_variant([], 'best')
        with self.assertRaises(PlaylistError):
            HLSDownloader.select_variant(variants, 'best', index=9)

    def test_master_playlist_selects_best(self) -> None:
        """master プレイリストから最高画質のバリアントが自動選択されること。"""
        low = b'L' * 100
        high = b'H' * 100
        self.server.routes['/low.m3u8'] = _text(_media_playlist(['/low_seg.ts']))
        self.server.routes['/high.m3u8'] = _text(_media_playlist(['/high_seg.ts']))
        self.server.routes['/low_seg.ts'] = _static(low)
        self.server.routes['/high_seg.ts'] = _static(high)
        master = (
            '#EXTM3U\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=300000,RESOLUTION=640x360\n'
            '/low.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n'
            '/high.m3u8\n'
        )
        self.server.routes['/master.m3u8'] = _text(master)

        options = DownloadOptions(output_path=self.tmp_path / 'out.mp4', merge_format='none')
        result = HLSDownloader(options).download(self.server.url('/master.m3u8'))

        self.assertEqual(result.read_bytes(), high)
        self.assertEqual(self.server.request_count('/low_seg.ts'), 0)


class TestMerge(unittest.TestCase):
    """結合処理と ffmpeg フォールバックを検証する。"""

    def setUp(self) -> None:
        """サーバーと一時ディレクトリを用意する。"""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = _TestServer()
        self.addCleanup(self.server.close)
        self.tmp_path = Path(self.tmp.name)

    def test_ffmpeg_missing_falls_back_to_ts(self) -> None:
        """ffmpeg が見つからない場合に .ts でバイナリ結合されること。"""
        payloads = [b'A' * 100, b'B' * 100]
        for index, payload in enumerate(payloads):
            self.server.routes[f'/seg{index}.ts'] = _static(payload)
        self.server.routes['/media.m3u8'] = _text(_media_playlist(['/seg0.ts', '/seg1.ts']))

        logs: list[str] = []
        options = DownloadOptions(
            output_path=self.tmp_path / 'out.mp4',
            merge_format='mp4',
            ffmpeg_path=str(self.tmp_path / 'does_not_exist' / 'ffmpeg.exe'),
        )
        result = HLSDownloader(options, log_callback=logs.append).download(self.server.url('/media.m3u8'))

        self.assertEqual(result.suffix, '.ts')
        self.assertEqual(result.read_bytes(), b''.join(payloads))
        self.assertTrue(any('バイナリ結合' in message for message in logs))


class TestLiveRecording(unittest.TestCase):
    """ライブ収録 (EXT-X-ENDLIST なし) を検証する。"""

    def test_live_recording_appends_new_segments(self) -> None:
        """再取得で増えたセグメントを追記し、ENDLIST で終了すること。"""
        with tempfile.TemporaryDirectory() as tmp:
            server = _TestServer()
            self.addCleanup(server.close)
            payloads = [bytes([65 + index]) * 128 for index in range(4)]
            for index, payload in enumerate(payloads):
                server.routes[f'/live{index}.ts'] = _static(payload)

            state = {'poll': 0}

            def provider() -> str:
                state['poll'] += 1
                if state['poll'] < 3:
                    return _media_playlist(['/live0.ts', '/live1.ts'], media_sequence=0, endlist=False)
                return _media_playlist(
                    ['/live0.ts', '/live1.ts', '/live2.ts', '/live3.ts'],
                    media_sequence=0,
                    endlist=True,
                )

            server.routes['/live.m3u8'] = _dynamic(provider)

            options = DownloadOptions(
                output_path=Path(tmp) / 'out.mp4',
                merge_format='none',
                live=True,
                live_poll_interval=0.05,
            )
            result = HLSDownloader(options).download(server.url('/live.m3u8'))

            self.assertEqual(result.read_bytes(), b''.join(payloads))


class TestFfmpegAvailable(unittest.TestCase):
    """ffmpeg が PATH にある場合の解決だけを確認する (実結合は別担当の領域)。"""

    def test_ffmpeg_is_resolvable(self) -> None:
        """PATH 上の ffmpeg を解決できること。"""
        found = shutil.which('ffmpeg')
        if found is None:
            self.skipTest('ffmpeg が PATH に無いためスキップ')
        with tempfile.TemporaryDirectory() as tmp:
            options = DownloadOptions(output_path=Path(tmp) / 'out.mp4', ffmpeg_path=found)
            downloader = HLSDownloader(options)
            self.assertEqual(downloader._resolve_ffmpeg(), found)


if __name__ == '__main__':
    unittest.main()
