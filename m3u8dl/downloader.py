"""HLS (m3u8) のダウンロード本体モジュール。

メディアプレイリストのセグメントを並列取得し、リトライ・AES-128 復号を行ったうえで
ffmpeg (またはバイナリ) で 1 本のファイルに結合する。EXT-X-ENDLIST を持たない
ライブ配信の収録にも対応する。

HTTP 取得は標準ライブラリの urllib.request を使用し、DownloadOptions.headers に
渡されたヘッダ (UA / Referer / Cookie など) をそのまま付与する。
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from m3u8dl.crypto_aes import aes128_cbc_decrypt, derive_iv
from m3u8dl.errors import DownloadCancelled, DownloadError, PlaylistError
from m3u8dl.hls import (
    ByteRange,
    MediaPlaylist,
    Segment,
    Variant,
    parse_media_playlist,
    parse_playlist,
)

# セグメント再取得の指数バックオフの基準秒 (0.5s, 1s, 2s, ...)
_RETRY_BACKOFF_BASE = 0.5
# speed_bps / eta_seconds を再計算する間隔 (秒)。これより短い間隔では前回値を保持する
_SPEED_UPDATE_INTERVAL = 1.0
# ffmpeg の結合処理に許容する最大秒数
_FFMPEG_TIMEOUT = 600


@dataclass
class DownloadOptions:
    """ダウンロード処理の設定。

    Attributes:
        output_path: 最終的な出力ファイルパス。結合形式により拡張子が変わる場合がある。
        headers: HTTP リクエストにそのまま付与するヘッダ (UA / Referer / Cookie など)。
        concurrency: セグメントの同時取得数。
        retries: 失敗セグメントの追加リトライ回数 (総試行回数は retries + 1)。
        timeout: 1 リクエストのタイムアウト秒。
        merge_format: 結合形式。'mp4' / 'ts' / 'none'。
        ffmpeg_path: ffmpeg 実行ファイルのパス。None なら PATH から探索する。
        keep_segments: True なら結合後も一時セグメントを削除しない。
        max_segments: 先頭から取得する最大セグメント数。None なら無制限。
        live: EXT-X-ENDLIST が無いプレイリストを収録し続けるか。
        live_poll_interval: ライブ収録で次のセグメントを取得するまでの待機秒。
        segment_dir: 一時セグメントの保存先。None なら output_path と同じ場所の <name>_segments。
    """

    output_path: Path
    headers: dict[str, str] = field(default_factory=dict)
    concurrency: int = 8
    retries: int = 3
    timeout: float = 20.0
    merge_format: str = 'mp4'
    ffmpeg_path: str | None = None
    keep_segments: bool = False
    max_segments: int | None = None
    live: bool = False
    live_poll_interval: float = 3.0
    segment_dir: Path | None = None


@dataclass
class DownloadProgress:
    """ダウンロードの進捗スナップショット。progress_callback に渡される。

    Attributes:
        phase: 現在の処理段階 ('fetch_playlist' / 'download' / 'merge' / 'done' / 'live_wait')。
        message: 人が読むための説明文。
        downloaded_segments: 取得済みセグメント数。
        total_segments: 想定される総セグメント数。
        downloaded_bytes: 取得済みバイト数。
        speed_bps: 直近 1 秒あたりの平均速度 (バイト/秒)。
        eta_seconds: 残り時間の推定値 (秒)。算出できないときは None。
    """

    phase: str
    message: str
    downloaded_segments: int = 0
    total_segments: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None


def _variant_score(variant: Variant) -> tuple[int, int]:
    """バリアントの画質を比較するためのスコアを返す。

    Args:
        variant: スコアを計算するバリアント。

    Returns:
        (帯域, 解像度の縦画素数) のタプル。同点時は解像度で比較する。
    """
    bandwidth = variant.bandwidth or variant.average_bandwidth or 0
    height = 0
    if variant.resolution and 'x' in variant.resolution:
        try:
            height = int(variant.resolution.split('x')[-1])
        except ValueError:
            height = 0
    return bandwidth, height


class HLSDownloader:
    """HLS プレイリストのダウンロードを実行するクラス。

    1 つのインスタンスで 1 回の download() を実行する想定だが、cancel() は
    別スレッドから呼ばれる。進捗はワーカースレッドから progress_callback へ通知する。
    """

    def __init__(
        self,
        options: DownloadOptions,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """ダウンローダーを初期化する。

        Args:
            options: ダウンロード設定。
            progress_callback: 進捗通知のコールバック。ワーカースレッドから呼ばれる。
            log_callback: ログ文字列を受け取るコールバック。
        """
        # 保持する情報: ダウンロード設定と各種コールバック
        self.options = options
        self._progress_callback = progress_callback
        self._log_callback = log_callback

        # 保持する情報: 一時セグメントの保存ディレクトリ (未指定なら output_path から導出)
        if options.segment_dir is not None:
            self._segment_dir = Path(options.segment_dir)
        else:
            self._segment_dir = options.output_path.parent / f'{options.output_path.stem}_segments'

        # キャンセル要求フラグ。download() の開始時に新しい Event で置き換える
        self._cancelled = threading.Event()
        # 暗号鍵 URL -> 鍵バイト列 のキャッシュ (同一鍵の再取得を避ける)
        self._key_cache: dict[str, bytes] = {}
        # 鍵キャッシュをスレッドセーフに扱うためのロック (並列取得時の二重取得を防ぐ)
        self._key_lock = threading.Lock()
        # 進捗カウンタをスレッドセーフに更新するためのロック
        self._counter_lock = threading.Lock()

        # 進捗管理用 (download() で初期化し直す)
        self._downloaded_segments = 0
        self._total_segments = 0
        self._bytes_done = 0
        self._start_time = 0.0
        self._last_speed_time = 0.0
        self._last_speed_bytes = 0
        self._speed_bps = 0.0
        self._eta_seconds: float | None = None

        # 参照されるメソッド: _merge() が結合対象として読む
        self._ordered_parts: list[Path] = []
        # fMP4 の初期化セグメント (#EXT-X-MAP) を保存した一時ファイル
        self._init_part: Path | None = None

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    def download(self, playlist_url: str) -> Path:
        """プレイリストを解析してダウンロードし、最終ファイルのパスを返す。

        Args:
            playlist_url: マスター/メディアのいずれのプレイリスト URL でもよい。
                マスターの場合は best のバリアントを自動選択する。

        Returns:
            結合後の最終ファイルパス。

        Raises:
            DownloadCancelled: cancel() により中断されたとき。
            DownloadError: 取得やファイル出力に失敗したとき。
            PlaylistError: プレイリストの解析に失敗したとき。
        """
        # 新しいダウンロードごとにキャンセルフラグと状態を初期化する
        self._cancelled = threading.Event()
        self._key_cache.clear()
        self._ordered_parts = []
        self._init_part = None

        self._emit('fetch_playlist', f'プレイリストを取得しています: {playlist_url}')
        text = self._fetch_text(playlist_url)
        kind, parsed = parse_playlist(text, playlist_url)

        # マスタープレイリストなら最良のバリアントのメディアプレイリストへ進む
        if kind == 'master':
            variant = self.select_variant(parsed, 'best')  # type: ignore[arg-type]
            self._emit('fetch_playlist', f'バリアントを取得しています: {variant.label or variant.url}')
            text = self._fetch_text(variant.url)
            playlist = parse_media_playlist(text, variant.url)
        else:
            playlist = parsed  # type: ignore[assignment]

        # SAMPLE-AES は未対応 (復号できないため早期に失敗させる)
        for segment in playlist.segments:
            method = (segment.key.method if segment.key else '') or ''
            if method.upper() == 'SAMPLE-AES':
                raise DownloadError('SAMPLE-AES is not supported')

        # fMP4 の初期化セグメントがあれば先に取得しておく
        self._download_init_segment(playlist)

        segments = list(playlist.segments)
        if self.options.max_segments is not None:
            segments = segments[: self.options.max_segments]

        if playlist.is_live and not self.options.live:
            # ENDLIST が無くライブ収録も指定されていない場合は取れる分だけ保存する
            self._log('プレイリストに #EXT-X-ENDLIST がありません。取得できた分だけ保存します')

        if playlist.is_live and self.options.live:
            parts = self._download_live(playlist.url, segments)
        else:
            parts = self._download_segments(segments)

        self._ordered_parts = parts
        return self._merge()

    def cancel(self) -> None:
        """進行中のダウンロードに中断を要求する。冪等。"""
        self._cancelled.set()

    @staticmethod
    def select_variant(
        variants: list[Variant],
        mode: str = 'best',
        index: int | None = None,
    ) -> Variant:
        """バリアント一覧から 1 つを選択する。

        Args:
            variants: 選択対象のバリアント一覧。
            mode: 'best' (最高画質) / 'worst' または 'lowest' (最低画質)。index 指定時は無視される。
            index: 手動選択するときのインデックス。指定時はこの値を優先する。

        Returns:
            選択されたバリアント。

        Raises:
            PlaylistError: バリアントが空、または index が範囲外のとき。
        """
        if not variants:
            # 呼び出し側の取り違えを早期に検出する
            raise PlaylistError('バリアントが存在しません')
        if index is not None:
            if index < 0 or index >= len(variants):
                raise PlaylistError(f'バリアントのインデックスが範囲外です: {index}')
            return variants[index]
        normalized = mode.strip().lower()
        if normalized in ('worst', 'lowest', 'low', 'min'):
            return min(variants, key=_variant_score)
        # 'best' / 'highest' / 不明な値は最高画質として扱う
        return max(variants, key=_variant_score)

    # ------------------------------------------------------------------
    # 進捗・ログ通知
    # ------------------------------------------------------------------

    def _log(self, message: str) -> None:
        """ログコールバックへメッセージを渡す。コールバック例外は握りつぶす。

        Args:
            message: 出力するログ文字列。
        """
        if self._log_callback is None:
            return
        try:
            self._log_callback(message)
        except Exception:  # noqa: BLE001 - GUI 側の例外でダウンロードを止めない
            pass

    def _emit(self, phase: str, message: str) -> None:
        """現在の進捗を progress_callback へ通知する。

        Args:
            phase: 処理段階。
            message: 説明文。
        """
        # セグメント単位の 'download' とポーリングごとの 'live_wait' は
        # ログが溢れるため progress_callback にのみ流し、ログには出さない
        if phase not in ('download', 'live_wait'):
            self._log(f'[{phase}] {message}')
        if self._progress_callback is None:
            return
        progress = DownloadProgress(
            phase=phase,
            message=message,
            downloaded_segments=self._downloaded_segments,
            total_segments=self._total_segments,
            downloaded_bytes=self._bytes_done,
            speed_bps=self._speed_bps,
            eta_seconds=self._eta_seconds,
        )
        try:
            self._progress_callback(progress)
        except Exception:  # noqa: BLE001 - GUI 側の例外でダウンロードを止めない
            pass

    def _update_speed(self) -> None:
        """速度と残り時間を 1 秒間隔で再計算する。"""
        now = time.monotonic()
        elapsed_since_update = now - self._last_speed_time
        # 1 秒未満の更新では前回の速度・ETA をそのまま使う (瞬間値の暴れを防ぐ)
        if elapsed_since_update < _SPEED_UPDATE_INTERVAL:
            return
        delta_bytes = self._bytes_done - self._last_speed_bytes
        if elapsed_since_update > 0:
            self._speed_bps = delta_bytes / elapsed_since_update
        self._last_speed_time = now
        self._last_speed_bytes = self._bytes_done

        # 残り時間は「1 セグメントあたりの平均所要時間 × 残りセグメント数」で推定する
        if self._downloaded_segments > 0 and self._total_segments > self._downloaded_segments:
            average_per_segment = (now - self._start_time) / self._downloaded_segments
            self._eta_seconds = average_per_segment * (self._total_segments - self._downloaded_segments)
        else:
            self._eta_seconds = None

    def _reset_progress(self, total_segments: int) -> None:
        """進捗カウンタを初期化する。

        Args:
            total_segments: 想定される総セグメント数。
        """
        self._downloaded_segments = 0
        self._total_segments = total_segments
        self._bytes_done = 0
        self._speed_bps = 0.0
        self._eta_seconds = None
        now = time.monotonic()
        self._start_time = now
        self._last_speed_time = now
        self._last_speed_bytes = 0

    # ------------------------------------------------------------------
    # HTTP 取得
    # ------------------------------------------------------------------

    def _fetch_bytes(self, url: str, byte_range: ByteRange | None = None) -> bytes:
        """URL からバイト列を取得する。1 回のみで、リトライは行わない。

        Args:
            url: 取得先 URL。
            byte_range: 部分取得する範囲。指定時は Range ヘッダを付与する。

        Returns:
            レスポンス本文。

        Raises:
            DownloadError: HTTP エラーや接続失敗のとき。
        """
        headers = dict(self.options.headers)
        if byte_range is not None:
            # 末尾は offset + length - 1 (HTTP の Range は両端を含む)
            end = byte_range.offset + byte_range.length - 1
            headers['Range'] = f'bytes={byte_range.offset}-{end}'
        request = urllib.request.Request(url, headers=headers, method='GET')
        try:
            with urllib.request.urlopen(request, timeout=self.options.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise DownloadError(f'HTTP {error.code} {error.reason}: {url}') from error
        except urllib.error.URLError as error:
            raise DownloadError(f'接続に失敗しました: {url} ({error.reason})') from error
        except OSError as error:
            raise DownloadError(f'接続に失敗しました: {url} ({error})') from error

    def _fetch_with_retries(self, url: str, byte_range: ByteRange | None, label: str) -> bytes:
        """指数バックオフ付きで URL を取得する。

        Args:
            url: 取得先 URL。
            byte_range: 部分取得する範囲。
            label: ログに出す対象名 (セグメント / 暗号鍵 など)。

        Returns:
            レスポンス本文。

        Raises:
            DownloadCancelled: 待機中または再試行前に cancel() されたとき。
            DownloadError: 全試行が失敗したとき。
        """
        attempts = max(1, self.options.retries + 1)
        last_error: DownloadError | None = None
        for attempt in range(attempts):
            if self._cancelled.is_set():
                raise DownloadCancelled('ダウンロードをキャンセルしました')
            try:
                return self._fetch_bytes(url, byte_range)
            except DownloadCancelled:
                raise
            except DownloadError as error:
                last_error = error
                if attempt < attempts - 1:
                    self._log(f'{label}の取得に失敗しました ({attempt + 1}/{self.options.retries}): {url} / {error}')
                    self._sleep_backoff(attempt)
        # ここに来るのは全試行が失敗した場合のみ
        if last_error is not None:
            raise last_error
        raise DownloadError(f'{label}を取得できませんでした: {url}')

    def _sleep_backoff(self, attempt: int) -> None:
        """指数バックオフの待機を行う。待機中もキャンセルを監視する。

        Args:
            attempt: 0 始まりの試行番号。待機時間は 0.5 * 2^attempt 秒。
        """
        delay = _RETRY_BACKOFF_BASE * (2 ** attempt)
        deadline = time.monotonic() + delay
        # 0.1 秒刻みで待機し、その間に cancel() されれば即座に抜ける
        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise DownloadCancelled('ダウンロードをキャンセルしました')
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _fetch_text(self, url: str) -> str:
        """プレイリストなどのテキストを取得する。

        Args:
            url: 取得先 URL。

        Returns:
            デコード済みの文字列。

        Raises:
            DownloadError: 取得に失敗したとき。
        """
        data = self._fetch_with_retries(url, None, 'プレイリスト')
        # プレイリストは通常 UTF-8。壊れていても読めるように順に試す
        for encoding in ('utf-8-sig', 'utf-8', 'shift_jis', 'latin-1'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode('utf-8', errors='replace')

    def _get_key(self, uri: str | None) -> bytes:
        """AES-128 の鍵を取得する (URL 単位でキャッシュ)。

        Args:
            uri: 鍵の URL。

        Returns:
            16 バイトの鍵。

        Raises:
            DownloadError: URL が無い、取得に失敗した、または鍵長が不正なとき。
        """
        if not uri:
            raise DownloadError('AES-128 の鍵 URL が指定されていません')
        # 並列取得時に複数スレッドが同時に鍵を取りに行かないようロックする
        with self._key_lock:
            cached = self._key_cache.get(uri)
            if cached is not None:
                return cached
            data = self._fetch_with_retries(uri, None, '暗号鍵')
            if len(data) != 16:
                raise DownloadError(f'暗号鍵の長さが不正です ({len(data)} バイト): {uri}')
            self._key_cache[uri] = data
            return data

    # ------------------------------------------------------------------
    # セグメント取得
    # ------------------------------------------------------------------

    def _ensure_segment_dir(self) -> None:
        """一時セグメント用ディレクトリを作成する。"""
        self._segment_dir.mkdir(parents=True, exist_ok=True)

    def _download_init_segment(self, playlist: MediaPlaylist) -> None:
        """#EXT-X-MAP の初期化セグメントを取得する (fMP4 用)。

        Args:
            playlist: 解析済みメディアプレイリスト。

        Raises:
            DownloadError: 取得に失敗したとき。
        """
        if not playlist.has_map or not playlist.init_url:
            return
        self._ensure_segment_dir()
        data = self._fetch_with_retries(playlist.init_url, None, '初期化セグメント')
        self._init_part = self._segment_dir / 'seg_init.part'
        self._init_part.write_bytes(data)
        self._log(f'初期化セグメントを取得しました: {len(data)} バイト')

    def _fetch_segment(self, segment: Segment) -> bytes:
        """1 セグメントを取得し、必要なら AES-128 で復号する。

        Args:
            segment: 取得対象のセグメント。

        Returns:
            復号済み (平文) のセグメント本文。

        Raises:
            DownloadCancelled: キャンセルされたとき。
            DownloadError: 取得や復号に失敗したとき。
        """
        data = self._fetch_with_retries(segment.url, segment.byte_range, 'セグメント')
        key = segment.key
        if key is None:
            return data
        method = (key.method or '').upper()
        if method == 'SAMPLE-AES':
            raise DownloadError('SAMPLE-AES is not supported')
        if method == 'AES-128':
            key_bytes = self._get_key(key.uri)
            # IV 未指定のときはセグメントのメディアシーケンス番号から生成する
            iv = key.iv if key.iv is not None else derive_iv(segment.media_sequence)
            data = aes128_cbc_decrypt(key_bytes, iv, data)
        return data

    def _download_one(self, index: int, segment: Segment) -> Path:
        """1 セグメントを取得して一時ファイルへ書き出す。

        Args:
            index: セグメントの並び順インデックス。
            segment: 取得対象のセグメント。

        Returns:
            書き出した一時ファイルのパス。

        Raises:
            DownloadCancelled: キャンセルされたとき。
            DownloadError: 取得に失敗したとき。
        """
        if self._cancelled.is_set():
            raise DownloadCancelled('ダウンロードをキャンセルしました')
        data = self._fetch_segment(segment)
        if self._cancelled.is_set():
            raise DownloadCancelled('ダウンロードをキャンセルしました')
        part_path = self._segment_dir / f'seg_{index:06d}.part'
        part_path.write_bytes(data)
        # 進捗カウンタはワーカースレッドから更新するためロックで保護する
        with self._counter_lock:
            self._downloaded_segments += 1
            self._bytes_done += len(data)
        self._update_speed()
        self._emit('download', f'{self._downloaded_segments}/{self._total_segments} セグメント取得済み')
        return part_path

    def _download_segments(self, segments: list[Segment]) -> list[Path]:
        """セグメント群を並列取得し、並び順どおりの一時ファイル一覧を返す。

        Args:
            segments: 取得対象のセグメント一覧。

        Returns:
            取得順ではなくインデックス順に並んだ一時ファイルのパス一覧 (#EXT-X-MAP があれば先頭)。

        Raises:
            DownloadCancelled: キャンセルされたとき。
            DownloadError: いずれかのセグメント取得が最終的に失敗したとき。
        """
        self._ensure_segment_dir()
        total = len(segments)
        if total == 0:
            raise DownloadError('ダウンロード対象のセグメントがありません')
        self._reset_progress(total)

        ordered: list[Path | None] = [None] * total
        workers = max(1, min(self.options.concurrency, total))
        self._log(f'{total} セグメントを {workers} 並列で取得します')
        self._emit('download', f'{total} セグメントを {workers} 並列で取得します')

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        try:
            futures: dict[concurrent.futures.Future[Path], int] = {}
            for index, segment in enumerate(segments):
                if self._cancelled.is_set():
                    raise DownloadCancelled('ダウンロードをキャンセルしました')
                futures[executor.submit(self._download_one, index, segment)] = index
            for future in concurrent.futures.as_completed(futures):
                index = futures[future]
                try:
                    part = future.result()
                except BaseException:
                    # 1 つでも失敗したら未着手のタスクを取り消して即座に伝播する
                    for pending in futures:
                        pending.cancel()
                    raise
                ordered[index] = part
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        parts = [part for part in ordered if part is not None]
        if self._init_part is not None:
            # 初期化セグメントは必ず先頭に置く
            parts = [self._init_part, *parts]
        return parts

    def _interruptible_sleep(self, seconds: float) -> None:
        """キャンセル可能な待機を行う。

        Args:
            seconds: 待機する秒数。

        Raises:
            DownloadCancelled: 待機中に cancel() されたとき。
        """
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self._cancelled.is_set():
                raise DownloadCancelled('ダウンロードをキャンセルしました')
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        if self._cancelled.is_set():
            raise DownloadCancelled('ダウンロードをキャンセルしました')

    def _download_live(self, media_url: str, initial_segments: list[Segment]) -> list[Path]:
        """ライブ配信を収録する。新しいセグメントを検出するたびに追記していく。

        Args:
            media_url: メディアプレイリストの URL (繰り返し再取得する)。
            initial_segments: 初回解析で得られたセグメント一覧。

        Returns:
            取得順に並んだ一時ファイルのパス一覧。

        Raises:
            DownloadCancelled: キャンセルされたとき。
            DownloadError: セグメント取得が最終的に失敗したとき。
        """
        self._ensure_segment_dir()
        self._reset_progress(len(initial_segments))
        self._log('ライブ収録を開始します')
        self._emit('download', 'ライブ収録を開始します')

        # 取得済みセグメントをメディアシーケンス番号で判定し、重複取得を防ぐ
        seen_sequences: set[int] = set()
        parts: list[Path] = []
        counter = 0
        segments = initial_segments
        is_live = True
        max_segments = self.options.max_segments

        while True:
            for segment in segments:
                if segment.media_sequence in seen_sequences:
                    continue
                if max_segments is not None and counter >= max_segments:
                    break
                if self._cancelled.is_set():
                    raise DownloadCancelled('ダウンロードをキャンセルしました')
                seen_sequences.add(segment.media_sequence)
                data = self._fetch_segment(segment)
                part_path = self._segment_dir / f'seg_{counter:06d}.part'
                part_path.write_bytes(data)
                parts.append(part_path)
                counter += 1
                with self._counter_lock:
                    self._downloaded_segments = counter
                    self._total_segments = max(self._total_segments, counter)
                    self._bytes_done += len(data)
                self._update_speed()
                self._emit('download', f'{counter} セグメント収録済み')

            # 最大セグメント数に達したら収録を終える
            if max_segments is not None and counter >= max_segments:
                break
            if not is_live:
                break

            self._emit('live_wait', f'{self.options.live_poll_interval} 秒待機して再取得します')
            self._interruptible_sleep(self.options.live_poll_interval)

            # プレイリストを再取得して新しいセグメントと ENDLIST の有無を確認する
            try:
                text = self._fetch_text(media_url)
                refreshed = parse_media_playlist(text, media_url)
            except DownloadError as error:
                # 一時的な取得失敗は次周期で回復する可能性があるので継続する
                self._log(f'ライブプレイリストの再取得に失敗しました (継続します): {error}')
                continue
            segments = refreshed.segments
            is_live = refreshed.is_live

        return parts

    # ------------------------------------------------------------------
    # 結合
    # ------------------------------------------------------------------

    def _resolve_ffmpeg(self) -> str | None:
        """使用する ffmpeg のパスを決定する。

        Returns:
            ffmpeg の実行ファイルパス。見つからないときは None。
        """
        if self.options.ffmpeg_path:
            candidate = Path(self.options.ffmpeg_path)
            if candidate.exists():
                return str(candidate)
            self._log(f'指定された ffmpeg が見つかりません: {candidate}')
            return None
        # 指定が無ければ PATH から探索する (同梱探索は親が ffmpeg_path で指定する)
        return shutil.which('ffmpeg')

    def _merge_with_ffmpeg(self, ffmpeg: str, parts: list[Path], target: Path, merge_format: str) -> None:
        """ffmpeg の concat デマルチプレクサで結合する。

        Args:
            ffmpeg: ffmpeg 実行ファイルのパス。
            parts: 結合対象の一時ファイル (この順に連結する)。
            target: 出力先ファイルパス。
            merge_format: 'mp4' のとき ADTS→ASC 変換フィルタを付ける。

        Raises:
            DownloadError: ffmpeg の実行または結合に失敗したとき。
        """
        list_path = self._segment_dir / 'concat_list.txt'
        lines = [f"file '{part.as_posix()}'" for part in parts]
        list_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)

        command = [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', str(list_path), '-c', 'copy']
        if merge_format == 'mp4':
            # mp4 は AAC を ADTS から ASC へ変換する必要がある (ts 出力では付けない)
            command.extend(['-bsf:a', 'aac_adtstoasc'])
        command.append(str(target))
        self._log('ffmpeg で結合します: ' + ' '.join(command))

        # GUI アプリなので Windows ではコンソール窓を出さないようにする
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DownloadError(f'ffmpeg の実行に失敗しました: {error}') from error
        if completed.returncode != 0:
            stderr = completed.stderr.decode('utf-8', errors='replace')[-500:]
            raise DownloadError(f'ffmpeg が終了コード {completed.returncode} で失敗しました: {stderr}')

    def _concat_binary(self, parts: list[Path], target: Path) -> Path:
        """セグメントをバイナリ結合して 1 ファイルにまとめる。

        Args:
            parts: 結合対象の一時ファイル (この順に連結する)。
            target: 出力先ファイルパス。

        Returns:
            出力先ファイルパス。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as output:
            for part in parts:
                with part.open('rb') as source:
                    shutil.copyfileobj(source, output)
        self._cleanup_segments(parts)
        return target

    def _cleanup_segments(self, parts: list[Path]) -> None:
        """結合後の一時ファイルを削除する。

        Args:
            parts: 削除対象の一時ファイル一覧。
        """
        if self.options.keep_segments:
            return
        for part in parts:
            try:
                part.unlink(missing_ok=True)
            except OSError as error:
                # 削除失敗は致命的ではないのでログだけ残す
                self._log(f'一時ファイルを削除できませんでした: {part} ({error})')
        # 作業用の concat リストも消しておく
        list_path = self._segment_dir / 'concat_list.txt'
        try:
            list_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _merge(self) -> Path:
        """取得済みセグメントを設定に従って 1 ファイルに結合する。

        Returns:
            最終ファイルのパス。

        Raises:
            DownloadError: 結合対象が無い、または結合に失敗したとき。
        """
        parts = [part for part in self._ordered_parts if part is not None]
        if not parts:
            raise DownloadError('結合するセグメントがありません')

        merge_format = (self.options.merge_format or 'mp4').lower()
        self._emit('merge', 'ファイルを結合しています')

        # 'none' は ffmpeg を使わずバイナリ結合のみ (.ts として保存する)
        if merge_format == 'none':
            target = self.options.output_path.with_suffix('.ts')
            result = self._concat_binary(parts, target)
            self._emit('done', f'保存しました: {result}')
            return result

        suffix = '.mp4' if merge_format == 'mp4' else '.ts'
        target = self.options.output_path.with_suffix(suffix)
        ffmpeg = self._resolve_ffmpeg()

        if ffmpeg is not None:
            try:
                self._merge_with_ffmpeg(ffmpeg, parts, target, merge_format)
                self._cleanup_segments(parts)
                self._emit('done', f'保存しました: {target}')
                return target
            except DownloadError as error:
                # ffmpeg が使えない場合はバイナリ結合へフォールバックする
                self._log(f'ffmpeg での結合に失敗しました。バイナリ結合にフォールバックします: {error}')
        else:
            self._log('ffmpeg が見つからないためバイナリ結合で保存します')

        # フォールバック: mp4 指定でも拡張子は .ts になる
        fallback = self.options.output_path.with_suffix('.ts')
        if suffix == '.mp4':
            self._log(f'mp4 指定でしたが ffmpeg が使えないため .ts として保存します: {fallback.name}')
        result = self._concat_binary(parts, fallback)
        self._emit('done', f'保存しました: {result}')
        return result
