# m3u8dl モジュール間インターフェース契約 (凍結版)

このファイルは並列実装するモジュール間の**唯一の契約**です。担当ファイル以外は編集しないこと。
契約に無い公開 API を勝手に増やさないこと（内部ヘルパー `_name` は自由）。

## 共通ルール

- 対象: Windows / Python 3.12 (`py -3.12`)。**標準ライブラリのみ**（pip install 禁止）。tkinter, urllib, socket, ssl, threading, concurrent.futures, json, re, dataclasses, pathlib, subprocess は使用可。
- パッケージ: リポジトリルート `J:\github\m3u8-downloader\` 直下の `m3u8dl\` パッケージ。
- コメント・Docstring は日本語。型ヒント必須。文字列は原則シングルクォート。
- `git` コマンドは実行しないこと（親がコミットする）。担当外ファイルを新規作成しないこと。
- 自分のテストだけを実行: リポジトリルートで `py -3.12 -m unittest discover -s tests -p "test_<担当>.py" -v`
- 例外クラスは `m3u8dl/errors.py` に定義済み（親が用意）: `M3U8DLError`, `PlaylistError`, `DownloadError`, `BrowserError`, `CancelledError`(= `DownloadCancelled`)。
  - `from m3u8dl.errors import PlaylistError, DownloadError, BrowserError, DownloadCancelled`

## `m3u8dl/hls.py` — プレイリスト解析 (担当B)

```python
@dataclass
class ByteRange:
    length: int
    offset: int

@dataclass
class EncryptionKey:
    method: str                    # 'AES-128' | 'SAMPLE-AES' | 'NONE'
    uri: str | None = None         # 絶対 URL に解決済み
    iv: bytes | None = None        # 16 バイト。未指定なら None (呼び出し側が sequence から生成)
    key_format: str | None = None

@dataclass
class Segment:
    url: str                       # 絶対 URL
    duration: float
    title: str | None = None
    byte_range: ByteRange | None = None
    key: EncryptionKey | None = None
    discontinuity: bool = False
    media_sequence: int = 0

@dataclass
class Variant:
    url: str                       # 絶対 URL
    bandwidth: int | None = None
    average_bandwidth: int | None = None
    resolution: str | None = None  # '1280x720'
    codecs: str | None = None
    name: str | None = None        # NAME 属性 or 解像度/帯域から生成したラベル
    frame_rate: float | None = None
    label: str = ''                # GUI 表示用 '720p 2149kbps 1280x720' 形式

@dataclass
class MediaPlaylist:
    url: str
    segments: list[Segment]
    media_sequence: int
    target_duration: float
    is_live: bool                  # EXT-X-ENDLIST が無ければ True
    version: int | None = None
    has_map: bool = False          # EXT-X-MAP (fMP4) を含むか
    total_duration: float = 0.0

def is_master_playlist(text: str) -> bool
def parse_master_playlist(text: str, base_url: str) -> list[Variant]   # 0 件なら PlaylistError
def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist
def parse_playlist(text: str, base_url: str) -> tuple[str, list[Variant] | MediaPlaylist]
    # -> ('master', [Variant...]) または ('media', MediaPlaylist)
def format_duration(seconds: float) -> str   # '01:23:45' / '12:34'
```

要件: `#EXT-X-STREAM-INF` の任意属性順、コメント行、空行、CRLF 対応。相対 URL は `urllib.parse.urljoin` で解決。
`#EXT-X-KEY` は後続セグメントに適用され続ける（`METHOD=NONE` で解除）。`#EXT-X-BYTERANGE` は次セグメントのみ有効。
`#EXT-X-MAP` の URL は `Segment` には入れず、`MediaPlaylist.has_map=True` と `Segment.url` の先頭セグメントに含めないこと（初期化セグメントは downloader が `#EXT-X-MAP` を再取得する）。※ `#EXT-X-MAP:URI=` を `init_url` として `MediaPlaylist` に追加フィールド `init_url: str | None = None` で保持すること。
`#EXT-X-DISCONTINUITY` は直後セグメントの `discontinuity=True`。

## `m3u8dl/crypto_aes.py` — AES-128-CBC 復号 (担当C)

```python
def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes   # PKCS#7 パディング除去あり(不正なら除去しない)
def derive_iv(media_sequence: int) -> bytes   # 16 バイト big-endian の IV
```

外部実装が利用可能ならそちらを優先してよい（`cryptography` / `Crypto` があれば `_backend` 変数に記録）が、**標準ライブラリのみで動く純 Python 実装が必須**。

## `m3u8dl/downloader.py` — ダウンロード本体 (担当C)

```python
@dataclass
class DownloadOptions:
    output_path: Path
    headers: dict[str, str] = field(default_factory=dict)   # UA/Referer/Cookie 等をそのまま HTTP ヘッダに使う
    concurrency: int = 8
    retries: int = 3
    timeout: float = 20.0
    merge_format: str = 'mp4'      # 'mp4' | 'ts' | 'none'
    ffmpeg_path: str | None = None
    keep_segments: bool = False
    max_segments: int | None = None    # テスト/部分保存用
    live: bool = False                 # ENDLIST が無いプレイリストを収録し続ける
    live_poll_interval: float = 3.0
    segment_dir: Path | None = None    # 省略時は output_path と同じ場所の <name>_segments

@dataclass
class DownloadProgress:
    phase: str                # 'fetch_playlist' | 'download' | 'merge' | 'done' | 'live_wait'
    message: str
    downloaded_segments: int = 0
    total_segments: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    eta_seconds: float | None = None

class HLSDownloader:
    def __init__(self, options: DownloadOptions,
                 progress_callback: Callable[[DownloadProgress], None] | None = None,
                 log_callback: Callable[[str], None] | None = None) -> None
    def download(self, playlist_url: str) -> Path        # 戻り値: 最終ファイルパス。キャンセル時 DownloadCancelled
    def cancel(self) -> None                             # 別スレッドから呼ばれる。冪等
    @staticmethod
    def select_variant(variants: list[Variant], mode: str = 'best', index: int | None = None) -> Variant
```

要件:
- 進行中は `progress_callback` を**ワーカースレッドから**呼ぶ（GUI 側が `root.after` するのでスレッド安全は不要）。
- セグメントは `concurrent.futures.ThreadPoolExecutor` で並列取得。**順序を保って**ファイルへ書く（一時ファイル `seg_%06d.part` → 完了順に関わらず index 順で結合）。
- 失敗セグメントは `retries` 回リトライ（指数バックオフ）。最終的に失敗したら `DownloadError`（メッセージに URL と HTTP ステータス）。
- AES-128 は `crypto_aes.aes128_cbc_decrypt`。IV 未指定時は `derive_iv(segment.media_sequence)`。`SAMPLE-AES` は `DownloadError('SAMPLE-AES is not supported')`。
- ライブ (`is_live=True`): `live=True` ならセグメント再取得ループ（`EXT-X-MEDIA-SEQUENCE` の増分だけ追記、`live_poll_interval` 待機）。`live=False` かつ ENDLIST なしなら取得できた分だけ保存し `log_callback` で警告。
- 結合: ffmpeg があれば `ffmpeg -y -f concat -safe 0 -i list.txt -c copy <out>`（`ts` なら `-bsf:a aac_adtstoasc` を付けない、`mp4` なら付ける）。ffmpeg が無い/失敗時はバイナリ結合（`mp4` 指定でも拡張子 `.ts` に警告付きでフォールバック）。
- 進捗: 1 秒間隔で `speed_bps`・`eta_seconds` を更新。
- 一時セグメントは成功時に削除（`keep_segments=True` 以外）。
- `ffmpeg_path` が None なら `shutil.which('ffmpeg')` → 無ければ同梱探索はしない（親が指定する）。
- HTTP 取得は `urllib.request` を使用（`headers` をそのまま付与、`byte_range` 指定時は `Range` ヘッダ）。

## `m3u8dl/cookies.py` — Cookie 処理 (担当C)

```python
@dataclass
class CookieEntry:
    name: str
    value: str
    domain: str = ''
    path: str = '/'

def parse_cookie_text(text: str) -> list[CookieEntry]
    # 'a=1; b=2' / Netscape cookies.txt / Chrome 拡張の JSON 配列 / 'Cookie: a=1; b=2' のいずれも受理
def to_cookie_header(entries: list[CookieEntry], url: str = '') -> str   # 'a=1; b=2'
def from_cdp_cookies(cookies: list[dict]) -> list[CookieEntry]           # CDP Network.getAllCookies の結果
def merge_entries(*groups: list[CookieEntry]) -> list[CookieEntry]       # 同名は後勝ち
def save_netscape(entries: list[CookieEntry], path: Path) -> None
def load_file(path: Path) -> list[CookieEntry]                            # 拡張子/内容で自動判別
```

## `m3u8dl/websocket_client.py` + `m3u8dl/browser.py` — CDP ブラウザ解析 (担当A)

```python
# websocket_client.py
class WebSocketConnection:
    def __init__(self, host: str, port: int, path: str, origin: str = 'http://localhost', timeout: float = 10.0) -> None
    def send_text(self, text: str) -> None
    def recv_text(self, timeout: float = 1.0) -> str | None   # タイムアウト時 None
    def close(self) -> None
    def __enter__/__exit__ 対応

# browser.py
@dataclass
class SniffedRequest:
    url: str
    kind: str        # 'playlist' | 'segment' | 'key' | 'other'
    resource_type: str
    method: str = 'GET'
    status: int | None = None

class BrowserSniffer:
    def __init__(self, on_request: Callable[[SniffedRequest], None], on_status: Callable[[str], None] | None = None,
                 browser_path: str | None = None, user_data_dir: Path | None = None,
                 port: int = 0,  # 0 = 空きポート自動
                 headless: bool = False, keep_open: bool = True,
                 drive_playback: bool = True) -> None  # True = ページの再生を自動操作する (再生ボタンのクリック / play())
    def start(self, url: str) -> None      # ブラウザ起動 + CDP 接続 + 解析開始（非同期スレッドで受信ループ）
    def stop(self) -> None                 # ブラウザ終了 + ソケット切断
    def is_running(self) -> bool
    def navigate(self, url: str) -> None
    def get_cookies(self, url: str | None = None) -> list[dict]   # CDP Network.getAllCookies 相当
    def get_page_title(self) -> str
    def get_browser_path(self) -> str
```
要件: ブラウザ探索順 = Chrome → Edge → Brave (`Program Files`, `Program Files (x86)`, `LOCALAPPDATA`)。起動引数に
`--remote-debugging-port=<port> --user-data-dir=<tmp> --no-first-run --no-default-browser-check --disable-features=Translate`。
`http://127.0.0.1:<port>/json/version` を最大 20 秒ポーリングして `webSocketDebuggerUrl` 取得 → `/json` の page ターゲットに接続。
`Network.enable` を送り `Network.requestWillBeSent` を監視。`.m3u8`/`.m3u`/`.mpd` を含む URL は `kind='playlist'`、`.ts`/`.m4s`/`.mp4`/`.aac`/`.key` はそれぞれ `segment`/`key`。
UA 偽装: `Network.setUserAgentOverride` は使わない（実ブラウザの UA を尊重）。
CDP の応答 (`id` 付き) とイベント (`method` 付き) を振り分ける。`send_command` は内部メソッド `_send_command(method, params, timeout)` でよい。
受信ループは daemon スレッド。`stop()` は socket を閉じて終了させる（ブロックしない）。

## `m3u8dl/gui.py` — GUI (担当D)

```python
def main() -> None    # tkinter アプリ起動。エントリポイント
def run_selftest(report_path: Path) -> int   # GUI を出さずモジュール結合検証。0=成功。結果は JSON で report_path に書く
```

GUI 要件 (全体を 1 ファイル, ttk 使用):
- 上段: URL 入力欄 + 「ブラウザで解析」ボタン + 「解析停止」ボタン。
- 検出結果: `ttk.Treeview`（列: 種類 / URL / ステータス）。ダブルクリックで一覧の URL を URL 欄へ転記。
- Cookie: 複数行テキスト欄（`a=1; b=2` 形式 or Netscape 形式を貼り付け）+ 「ブラウザからCookie取得」ボタン + 「ファイル読込」ボタン + チェックボックス「Cookie を使う」。
- 保存先: ファイルパス（`ttk.Entry` + 「参照」）。既定は `~/Downloads/<自動名>.mp4`。
- オプション: 画質 Combobox（自動(最高) / 自動(最低) / 手動選択）、同時接続数 Spinbox、ffmpeg で mp4 に変換 チェック、ライブ収録 チェック、最大セグメント数 Entry（空=無制限）。
- 下段: `ttk.Progressbar`（determinate）+ ステータスラベル（速度/残り時間/進捗）+ ログ `tk.Text`（読み取り専用）+ 「ダウンロード開始」「キャンセル」ボタン。
- スレッド: 解析・本体 DL は `threading.Thread(daemon=True)`。UI 更新は必ず `root.after(0, lambda ...)`。
- 実行中は入力欄/ボタンを `state='disabled'`、終了で戻す（二重実行ガード）。
- ログは日時付き。例外はスタックトレースをログ欄に出しつつ `messagebox.showerror`。
- マスタープレイリスト検出時は画質選択 Combobox に variant を列挙（`Variant.label`）。
- ffmpeg 探索: `shutil.which('ffmpeg')`、無ければ `ffmpeg_path` を空にして mp4 変換を無効化＋ログ警告。
- `--selftest <path>` 起動引数で `run_selftest` を実行し終了（GUI を出さない）。exe 検証で使う。
