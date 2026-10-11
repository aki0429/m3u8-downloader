"""CDP (Chrome DevTools Protocol) を使ってブラウザの通信を解析するモジュール。

Chrome / Edge / Brave をリモートデバッグポート付き・専用ユーザーデータディレクトリで起動し、
ページが発行するネットワークリクエストのうち m3u8 プレイリストやセグメントを検出する。
Cookie の取得やページタイトルの取得も CDP 経由で行う。

ブラウザの起動・CDP 接続・受信ループ (daemon スレッド) を担当し、検出結果は
`on_request` コールバックで通知する。受信ループは 1 スレッドだけが WebSocket を読むので、
`_send_command()` は同じ受信ループが配る応答を `threading.Event` で待つ。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable
from urllib.parse import urlparse

from m3u8dl.errors import BrowserError
from m3u8dl.websocket_client import WebSocketConnection

# プロキシ環境変数の影響を受けずに 127.0.0.1 のデバッグ HTTP エンドポイントへ接続するための opener
_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# URL の種別判定に使う拡張子マーカー (判定は URL のパス部分の小文字に対して行う)
_PLAYLIST_MARKERS = ('.m3u8', '.m3u', '.mpd')
_KEY_MARKERS = ('.key',)
_SEGMENT_MARKERS = ('.ts', '.m4s', '.mp4', '.aac')

# デバッグポートと page ターゲットを待つ最大時間 (秒)
_DEBUGGER_WAIT_SECONDS = 20.0

# 起動したプロセスが終了しても、デバッグポートの応答を待ち続ける時間 (秒)。
# 更新適用のための再起動や、別プロセスへ処理を引き継いで元のプロセスだけが終了する場合があるため、
# プロセスの終了だけで「起動失敗」と決めつけない。
_POST_EXIT_GRACE_SECONDS = 5.0

# ブラウザを親プロセスから完全に切り離して起動するための生成フラグ (Windows のみ)。
# Chromium 系ブラウザは、親のコンソールやジョブオブジェクトに繋がった状態で起動されると、
# 何も出力せずに終了コード 0 で即終了することがある (親がコンソールを持たない GUI アプリのときに起きやすい)。
# DETACHED_PROCESS でコンソールの引き継ぎを断ち、CREATE_BREAKAWAY_FROM_JOB で
# 「親の終了で巻き込まれる」ジョブオブジェクトから離脱させる。
_BROWSER_CREATION_FLAGS = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
) if os.name == 'nt' else 0

# CREATE_BREAKAWAY_FROM_JOB はジョブオブジェクト側が離脱を許可していないと失敗するため、
# それを外した生成フラグも用意する (この場合はコンソールの切り離しだけを行う)。
_BROWSER_CREATION_FLAGS_FALLBACK = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
) if os.name == 'nt' else 0


def _launch_variants() -> list[tuple[str, tuple[str, ...]]]:
    """ブラウザの起動引数のバリエーションを試行順に返す。

    1 つ目の「既定」で失敗した場合に備え、サンドボックスを無効化した起動も試せるようにしている。
    セキュリティ製品や制限された実行環境では、既定の起動が何も出力せずに即終了することがある。

    Returns:
        (表示名, 追加引数) のリスト (試行順)。
    """
    return [
        ('既定', ()),
        # 専用の一時プロファイルしか使わないため、フォールバックとしての影響は限定的
        ('sandbox 無効', ('--no-sandbox', '--disable-gpu')),
    ]


@dataclass
class SniffedRequest:
    """検出したネットワークリクエストを表す。

    Attributes:
        url: リクエスト URL。
        kind: 種別 ('playlist' | 'segment' | 'key' | 'other')。
        resource_type: CDP の ResourceType (例: 'XHR', 'Fetch', 'Media')。
        method: HTTP メソッド。
        status: 応答の HTTP ステータス。未確定のときは None。
    """

    url: str
    kind: str
    resource_type: str
    method: str = 'GET'
    status: int | None = None


def _classify(url: str) -> str:
    """URL を種別 ('playlist' | 'segment' | 'key' | 'other') に分類する。

    Args:
        url: 判定対象の URL。

    Returns:
        種別文字列。
    """
    # クエリ文字列による誤判定を避けるため、パス部分だけを小文字にして判定する
    path = urlparse(url).path.lower()
    if not path:
        path = url.lower()
    for marker in _PLAYLIST_MARKERS:
        if marker in path:
            return 'playlist'
    for marker in _KEY_MARKERS:
        if marker in path:
            return 'key'
    for marker in _SEGMENT_MARKERS:
        if marker in path:
            return 'segment'
    return 'other'


def _browser_candidates() -> list[str]:
    """探索するブラウザ実行ファイルのパス候補を優先順に返す。

    Returns:
        候補パスのリスト (Chrome → Edge → Brave の順)。
    """
    program_files = os.environ.get('PROGRAMFILES', r'C:\Program Files')
    program_files_x86 = os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')
    local_appdata = os.environ.get('LOCALAPPDATA', os.path.expanduser(r'~\AppData\Local'))
    candidates: list[str] = []
    # Chrome → Edge → Brave の順に、Program Files / Program Files (x86) / LOCALAPPDATA を探索する
    for base in (program_files, program_files_x86, local_appdata):
        candidates.append(os.path.join(base, 'Google', 'Chrome', 'Application', 'chrome.exe'))
    for base in (program_files, program_files_x86, local_appdata):
        candidates.append(os.path.join(base, 'Microsoft', 'Edge', 'Application', 'msedge.exe'))
    for base in (program_files, program_files_x86, local_appdata):
        candidates.append(os.path.join(base, 'BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe'))
    return candidates


def _find_browser_path() -> str | None:
    """インストール済みブラウザのうち、最初に見つかった実行ファイルのパスを返す。

    Returns:
        実行ファイルのパス。見つからなければ None。
    """
    for candidate in _browser_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_free_port() -> int:
    """ローカルで空いている TCP ポートを 1 つ確保して返す。

    Returns:
        空きポート番号。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return int(sock.getsockname()[1])


def _http_get_json(port: int, path: str, timeout: float = 1.0) -> object:
    """デバッグ HTTP エンドポイントに GET して JSON を返す。

    Args:
        port: デバッグポート。
        path: リクエストパス ('/json' など)。
        timeout: タイムアウト (秒)。

    Returns:
        パース済みの JSON 値。

    Raises:
        OSError: 接続に失敗したとき。
        ValueError: JSON のパースに失敗したとき。
    """
    url = f'http://127.0.0.1:{port}{path}'
    with _HTTP_OPENER.open(url, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode('utf-8'))


class BrowserSniffer:
    """ブラウザを起動して CDP で通信を解析するスニッファ。

    起動時に専用のユーザーデータディレクトリを使うため、既に起動中のブラウザとは干渉しない。
    受信ループは daemon スレッドで動き、`stop()` でソケットを閉じて終了させる。

    保持する情報:
        _proc: 起動したブラウザプロセス。`stop()` で終了させる。
        _ws: CDP の page ターゲットへの WebSocket 接続。
        _pending: 送信済み CDP コマンドの応答待ちスロット (id → slot)。
        _requests: requestId → 検出済み SniffedRequest (応答ステータスの紐付け用)。
    """

    def __init__(
        self,
        on_request: Callable[[SniffedRequest], None],
        on_status: Callable[[str], None] | None = None,
        browser_path: str | None = None,
        user_data_dir: Path | None = None,
        port: int = 0,
        headless: bool = False,
        keep_open: bool = True,
    ) -> None:
        """スニッファを初期化する (ブラウザの起動は `start()` で行う)。

        Args:
            on_request: リクエストを検出するたびに呼ぶコールバック。
            on_status: 起動・停止などの状態メッセージを受け取るコールバック。
            browser_path: 使用するブラウザの実行ファイルパス。None なら自動探索する。
            user_data_dir: ユーザーデータディレクトリ。None なら一時ディレクトリを作る。
            port: リモートデバッグポート。0 なら空きポートを自動確保する。
            headless: True ならヘッドレスモードで起動する。
            keep_open: True なら接続が切れても自動では後始末しない (呼び出し側が stop する)。
        """
        self._on_request = on_request
        self._on_status = on_status
        self._browser_path = browser_path
        self._user_data_dir = Path(user_data_dir) if user_data_dir is not None else None
        self._port = port
        self._headless = headless
        self._keep_open = keep_open

        # 起動時に解決しておく (実行ファイルが無ければ start() でエラーにする)
        self._resolved_browser_path = browser_path or _find_browser_path()

        self._proc: subprocess.Popen[bytes] | None = None
        self._ws: WebSocketConnection | None = None
        self._recv_thread: threading.Thread | None = None
        self._running = False
        # 送信済みコマンドの応答待ちスロット。受信ループが応答を入れて Event を立てる。
        self._pending: dict[int, dict[str, object]] = {}
        self._pending_lock = threading.Lock()
        self._id_counter = 0
        self._id_lock = threading.Lock()
        # requestId → 検出済みリクエスト (responseReceived でステータスを紐付けるため)
        self._requests: dict[str, SniffedRequest] = {}
        self._requests_lock = threading.Lock()
        # 自前で作った一時プロファイルディレクトリ (stop() で削除する)
        self._temp_profile: str | None = None
        # 今回の試行で使っているプロファイルディレクトリ (DevToolsActivePort の参照に使う)
        self._profile: Path | None = None
        # ブラウザの出力を逃がすログファイル (起動失敗時の原因表示に使う)
        self._launch_log: Path | None = None

    def start(self, url: str) -> None:
        """ブラウザを起動し、CDP に接続して `url` を開き、解析を開始する。

        起動に失敗したときは次の候補ブラウザ (Chrome → Edge → Brave) で自動的に再試行する。
        Chrome が更新中だったり、特定ブラウザだけがブロックされている環境でも解析を始められるようにするため。

        Args:
            url: 最初に開く URL。

        Raises:
            BrowserError: すべての候補ブラウザで失敗したとき。
        """
        if self._running:
            raise BrowserError('既に解析を開始しています')

        # 試すブラウザを決める。明示指定があればそれだけ、無ければ Chrome → Edge → Brave の順に試す。
        if self._browser_path:
            candidates = [self._browser_path]
        else:
            candidates = [candidate for candidate in _browser_candidates() if os.path.isfile(candidate)]
        if not candidates:
            raise BrowserError('対応ブラウザ (Chrome / Edge / Brave) が見つかりません')

        # ポートを決める (0 指定なら空きポートを確保する)
        port = self._port if self._port and self._port > 0 else _find_free_port()
        self._port = port

        failures: list[str] = []
        # 「ブラウザ × 起動引数のバリエーション」を順に試す
        attempts = [
            (candidate, variant, extra_arguments)
            for candidate in candidates
            for variant, extra_arguments in _launch_variants()
        ]
        for index, (candidate, variant, extra_arguments) in enumerate(attempts):
            try:
                self._launch_and_connect(candidate, url, port, extra_arguments)
                return
            except (BrowserError, OSError) as exc:
                # 起動しかけたプロセスとソケットを片付けてから次の候補へ進む
                failures.append(f'{os.path.basename(candidate)} ({variant}): {exc}')
                self.stop()
                if index + 1 < len(attempts):
                    next_candidate, next_variant, _ = attempts[index + 1]
                    self._emit_status(
                        f'{os.path.basename(candidate)} では解析を開始できませんでした。'
                        f'{os.path.basename(next_candidate)} ({next_variant}) で再試行します...'
                    )
        raise BrowserError('ブラウザ解析を開始できませんでした:\n' + '\n'.join(failures))

    def _spawn_browser(
        self,
        arguments: list[str],
        log_stream: IO[str] | None,
    ) -> tuple[subprocess.Popen[bytes], int]:
        """ブラウザのプロセスを起動する (生成フラグを順に試す)。

        ジョブオブジェクトからの離脱を許可していない環境があるため、離脱ありの生成フラグで失敗したら
        離脱なしの生成フラグで再試行する (コンソールの切り離しはどちらでも行う)。

        Args:
            arguments: 起動するコマンドライン引数。
            log_stream: ブラウザの出力を書き出すファイル (None なら出力を破棄する)。

        Returns:
            (起動したプロセス, 実際に使った生成フラグ)。

        Raises:
            BrowserError: どの生成フラグでもプロセスを起動できなかったとき。
        """
        # 同じフラグを 2 回試さないようにしつつ、試行順を保つ
        flag_candidates = list(dict.fromkeys((_BROWSER_CREATION_FLAGS, _BROWSER_CREATION_FLAGS_FALLBACK)))
        last_error: OSError | None = None
        for spawn_flags in flag_candidates:
            try:
                proc = subprocess.Popen(
                    arguments,
                    stdout=log_stream if log_stream is not None else subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    creationflags=spawn_flags,
                )
            except OSError as exc:
                # 生成フラグが原因の失敗 (離脱の拒否など) なら次の候補を試す
                last_error = exc
                continue
            return proc, spawn_flags
        raise BrowserError(f'ブラウザの起動に失敗しました: {last_error}')

    def _launch_and_connect(
        self,
        browser_path: str,
        url: str,
        port: int,
        extra_arguments: tuple[str, ...] = (),
    ) -> None:
        """指定ブラウザを起動して CDP に接続し、解析を開始する。

        Args:
            browser_path: 起動するブラウザの実行ファイルパス。
            url: 最初に開く URL。
            port: リモートデバッグポート。
            extra_arguments: 既定の引数に追加する起動引数 (起動バリエーション用)。

        Raises:
            BrowserError: 起動・接続・解析開始のいずれかに失敗したとき。
            OSError: ブラウザの実行ファイルを起動できなかったとき。
        """
        self._resolved_browser_path = browser_path

        # ユーザーデータディレクトリを用意する。省略時は専用の一時ディレクトリを作る。
        if self._user_data_dir is not None:
            profile = self._user_data_dir
            profile.mkdir(parents=True, exist_ok=True)
        else:
            profile = Path(tempfile.mkdtemp(prefix='m3u8dl_browser_'))
            self._temp_profile = str(profile)
        self._profile = profile

        arguments = [
            self._resolved_browser_path,
            f'--remote-debugging-port={port}',
            f'--user-data-dir={profile}',
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-features=Translate',
            # 127.0.0.1 への WebSocket 接続で Origin を送っても拒否されないようにする
            '--remote-allow-origins=*',
            '--noerrdialogs',
            '--disable-background-networking',
            # クリック操作を要求するプレーヤーでも再生が始まるようにする (解析の検出率を上げるため)
            '--autoplay-policy=no-user-gesture-required',
            # 解析中に音を鳴らさない
            '--mute-audio',
        ]
        # 起動バリエーションの追加引数 (サンドボックス無効化など) を足す
        arguments.extend(extra_arguments)
        if self._headless:
            arguments.append('--headless=new')
        arguments.append('about:blank')

        # ブラウザの出力をログファイルへ逃がす (起動直後に終了したときの原因究明に使う)
        log_path = Path(tempfile.gettempdir()) / 'm3u8dl_browser_launch.log'
        self._launch_log = log_path
        log_stream = None
        try:
            log_stream = log_path.open('a', encoding='utf-8', errors='replace')
            log_stream.write(f'\n=== {time.strftime("%Y-%m-%d %H:%M:%S")} {browser_path}\n')
            log_stream.write('args: ' + ' '.join(arguments[1:]) + '\n')
            log_stream.flush()
        except OSError:
            log_stream = None

        # Windows では親から完全に切り離して起動する (切り離さないとブラウザが即終了することがある)
        self._emit_status('ブラウザを起動しています...')
        try:
            self._proc, spawn_flags = self._spawn_browser(arguments, log_stream)
        except BrowserError:
            # 起動できなかったので、この試行で作った一時プロファイルを片付ける
            if log_stream is not None:
                log_stream.close()
            self._cleanup_profile()
            raise
        if log_stream is not None:
            # 実際に使った生成フラグを記録してから閉じる (切り分け用)
            log_stream.write(f'spawn flags: {spawn_flags:#x}\n')
            log_stream.flush()
            # 子プロセスがハンドルを引き継いだので親側は閉じる (ログは追記され続ける)
            log_stream.close()

        try:
            self._wait_for_debugger(port)
            self._emit_status('CDP に接続しています...')
            # ブラウザが実際に使ったポートで接続する (通常は port と同じ)
            ws_url = self._find_page_target(self._port)
            parsed = urlparse(ws_url)
            connection = WebSocketConnection(
                parsed.hostname or '127.0.0.1',
                parsed.port or port,
                parsed.path or '/',
                timeout=10.0,
            )
            self._ws = connection
            self._running = True
            # 受信ループを先に起動してから CDP コマンドを送る (応答は受信ループが配る)
            self._recv_thread = threading.Thread(target=self._receive_loop, name='m3u8dl-cdp', daemon=True)
            self._recv_thread.start()
            # ネットワーク監視を有効化してから対象ページへ遷移する
            self._send_command('Network.enable', {}, timeout=10.0)
            try:
                self._send_command('Page.enable', {}, timeout=5.0)
            except BrowserError:
                # Page ドメインは必須ではないので有効化に失敗しても続行する
                pass
            self.navigate(url)
            self._emit_status(f'ブラウザ解析を開始しました: {url}')
        except Exception:
            # 途中で失敗したら起動済みのプロセスとソケットを確実に片付ける
            self.stop()
            raise

    def stop(self) -> None:
        """解析を停止し、ブラウザを終了してソケットを切断する。冪等。"""
        # 何も動いていないときに呼ばれた場合は状態メッセージを出さない
        # (起動失敗の処理中に 2 回呼ばれるため、ログが二重に流れるのを防ぐ)
        was_active = self._running or self._proc is not None or self._ws is not None
        self._running = False

        # 受信ループが読んでいるソケットを閉じる (ブロックしない)
        connection = self._ws
        self._ws = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

        # 受信ループの終了を待つ (短いタイムアウト)
        thread = self._recv_thread
        self._recv_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

        # ブラウザプロセスを終了する
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            self._terminate_process(proc)

        self._cleanup_profile()
        with self._requests_lock:
            self._requests.clear()
        if was_active:
            self._emit_status('ブラウザ解析を停止しました')

    def is_running(self) -> bool:
        """解析中 (ブラウザと CDP 接続が生きている) かどうかを返す。

        Returns:
            解析中なら True。
        """
        if not self._running:
            return False
        if self._proc is None or self._proc.poll() is not None:
            return False
        if self._ws is None or self._ws.closed:
            return False
        return True

    def navigate(self, url: str) -> None:
        """現在の page ターゲットを指定 URL へ遷移させる。

        Args:
            url: 遷移先 URL。

        Raises:
            BrowserError: CDP に接続していない、またはコマンドが失敗したとき。
        """
        self._send_command('Page.navigate', {'url': url}, timeout=10.0)

    def get_cookies(self, url: str | None = None) -> list[dict]:
        """ブラウザの Cookie を取得する。

        Args:
            url: 指定するとその URL に該当する Cookie だけを取得する。None なら全 Cookie。

        Returns:
            CDP 形式の Cookie 辞書のリスト。

        Raises:
            BrowserError: CDP に接続していない、またはコマンドが失敗したとき。
        """
        if url:
            result = self._send_command('Network.getCookies', {'urls': [url]}, timeout=10.0)
        else:
            result = self._send_command('Network.getAllCookies', {}, timeout=10.0)
        if isinstance(result, dict):
            cookies = result.get('cookies')
            if isinstance(cookies, list):
                return [cookie for cookie in cookies if isinstance(cookie, dict)]
        return []

    def get_page_title(self) -> str:
        """現在のページのタイトルを取得する。

        Returns:
            ページタイトル。取得できなければ空文字。

        Raises:
            BrowserError: CDP に接続していない、またはコマンドが失敗したとき。
        """
        result = self._send_command(
            'Runtime.evaluate',
            {'expression': 'document.title', 'returnByValue': True},
            timeout=10.0,
        )
        if isinstance(result, dict):
            inner = result.get('result')
            if isinstance(inner, dict):
                value = inner.get('value')
                if isinstance(value, str):
                    return value
        return ''

    def get_browser_path(self) -> str:
        """使用する (使用した) ブラウザの実行ファイルパスを返す。

        Returns:
            実行ファイルパス。見つからなければ空文字。
        """
        return self._resolved_browser_path or ''

    def _wait_for_debugger(self, port: int) -> None:
        """デバッグポートが応答するまで (最大 20 秒) ポーリングする。

        起動したプロセスが先に終了した場合でも、すぐには失敗としない。ブラウザは更新の適用や
        処理の引き継ぎのために、起動直後のプロセスだけが終了することがあるため、
        `_POST_EXIT_GRACE_SECONDS` の間はデバッグポートの応答を待ち続ける。
        それでも応答が無ければ、終了コード・ブラウザの出力・一時プロファイルの状態を添えて報告する。

        Args:
            port: デバッグポート。

        Raises:
            BrowserError: ブラウザが途中で終了した、または 20 秒応答が無いとき。
        """
        deadline = time.monotonic() + _DEBUGGER_WAIT_SECONDS
        exited_at: float | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                if exited_at is None:
                    # プロセスの終了自体は失敗と決めつけず、ポートの応答を待ち続ける
                    exited_at = time.monotonic()
                    self._emit_status('ブラウザの起動プロセスが終了しました。デバッグポートの応答を待っています...')
                elif time.monotonic() - exited_at > _POST_EXIT_GRACE_SECONDS:
                    raise BrowserError(
                        f'ブラウザが起動直後に終了しました (exit code={self._proc.returncode})。'
                        'Chrome/Edge の更新中やセキュリティ製品・ポリシーによる制限の可能性があります。'
                        f'\n{self._profile_report()}'
                        f'{self._launch_log_tail()}'
                    )
            # ブラウザが自前で決めたポートで待ち受けている場合に備えて DevToolsActivePort の値も見る
            active_port = self._read_devtools_active_port()
            candidate_ports = [active_port, port] if active_port is not None and active_port != port else [port]
            for candidate_port in candidate_ports:
                try:
                    version = _http_get_json(candidate_port, '/json/version', timeout=1.0)
                except (OSError, ValueError):
                    continue
                if isinstance(version, dict) and version.get('webSocketDebuggerUrl'):
                    # 以降の接続もブラウザが実際に使ったポートに合わせる
                    self._port = candidate_port
                    return
            time.sleep(0.2)
        raise BrowserError(
            f'デバッグポートの応答を {_DEBUGGER_WAIT_SECONDS:.0f} 秒待ちましたが応答がありません。'
            f'\n{self._profile_report()}'
        )

    def _read_devtools_active_port(self) -> int | None:
        """プロファイル内の DevToolsActivePort からブラウザが実際に使っているポートを読む。

        ブラウザは起動時にこのファイルへ実際のポートを書き出すため、指定ポートを無視された
        場合でも接続先を特定できる。

        Returns:
            ポート番号。ファイルが無い・読めない・内容が不正なときは None。
        """
        if self._profile is None:
            return None
        try:
            lines = self._profile.joinpath('DevToolsActivePort').read_text(encoding='ascii', errors='replace').splitlines()
        except OSError:
            return None
        if not lines:
            return None
        first_line = lines[0].strip()
        return int(first_line) if first_line.isdigit() else None

    def _profile_report(self) -> str:
        """一時プロファイルの状態をエラーメッセージ用に 1 行で要約する。

        ブラウザが本当に起動処理を進めたのか (プロファイルを作ったのか) を切り分けるために使う。
        ファイルが 1 つも無ければ、ブラウザは初期化前に終了している。

        Returns:
            プロファイルのパス・作成されたファイル数・DevToolsActivePort の有無。
        """
        if self._profile is None:
            return '一時プロファイル: 未作成'
        try:
            names = sorted(entry.name for entry in self._profile.iterdir())
        except OSError as exc:
            return f'一時プロファイル: {self._profile} (内容を読めません: {exc})'
        active_port = 'DevToolsActivePort あり' if 'DevToolsActivePort' in names else 'DevToolsActivePort なし'
        head = ', '.join(names[:6])
        return f'一時プロファイル: {self._profile} / ファイル {len(names)} 件 [{head}] / {active_port}'

    def _launch_log_tail(self, length: int = 800) -> str:
        """ブラウザ起動ログの末尾をエラーメッセージに添える形で取り出す。

        Args:
            length: 添える最大文字数。

        Returns:
            ログファイルのパスと末尾。ログが無ければ空文字。
        """
        if self._launch_log is None:
            return ''
        try:
            text = self._launch_log.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''
        tail = text.strip()[-length:]
        if not tail:
            return ''
        return f'\n--- ブラウザの出力 ({self._launch_log}) ---\n{tail}'

    def _find_page_target(self, port: int) -> str:
        """page ターゲットの WebSocket URL が取得できるまで (最大 20 秒) ポーリングする。

        Args:
            port: デバッグポート。

        Returns:
            page ターゲットの webSocketDebuggerUrl。

        Raises:
            BrowserError: page ターゲットが見つからないとき。
        """
        deadline = time.monotonic() + _DEBUGGER_WAIT_SECONDS
        while time.monotonic() < deadline:
            # ここではデバッグポートは応答済み。起動プロセスの終了だけで打ち切らない
            # (更新適用や処理の引き継ぎでプロセスだけが入れ替わることがあるため)。
            # ブラウザが持つ page ターゲットは、実際に待ち受けているポートから探す
            active_port = self._read_devtools_active_port()
            for candidate_port in ((active_port, port) if active_port is not None and active_port != port else (port,)):
                try:
                    targets = _http_get_json(candidate_port, '/json', timeout=1.0)
                except (OSError, ValueError):
                    continue
                if isinstance(targets, list):
                    for target in targets:
                        if not isinstance(target, dict):
                            continue
                        if target.get('type') == 'page' and target.get('webSocketDebuggerUrl'):
                            return str(target['webSocketDebuggerUrl'])
            time.sleep(0.2)
        raise BrowserError('CDP の page ターゲットが見つかりません')

    def _send_command(self, method: str, params: dict | None = None, timeout: float = 10.0) -> object:
        """CDP コマンドを送信し、対応する応答の result を返す。

        Args:
            method: CDP メソッド名 (例: 'Network.enable')。
            params: パラメータ辞書。
            timeout: 応答を待つ最大時間 (秒)。

        Returns:
            応答の result 値。

        Raises:
            BrowserError: 未接続、タイムアウト、または応答が error のとき。
        """
        connection = self._ws
        if connection is None:
            raise BrowserError('CDP に接続していません')

        with self._id_lock:
            self._id_counter += 1
            command_id = self._id_counter
        slot: dict[str, object] = {'event': threading.Event(), 'result': None, 'error': None}
        with self._pending_lock:
            self._pending[command_id] = slot

        message = json.dumps({'id': command_id, 'method': method, 'params': params or {}})
        try:
            connection.send_text(message)
        except BrowserError:
            with self._pending_lock:
                self._pending.pop(command_id, None)
            raise

        event = slot['event']
        assert isinstance(event, threading.Event)
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(command_id, None)
            raise BrowserError(f'CDP コマンドがタイムアウトしました: {method}')
        if slot['error'] is not None:
            raise BrowserError(f'CDP コマンドがエラーを返しました: {method}: {slot["error"]}')
        return slot['result']

    def _receive_loop(self) -> None:
        """CDP のメッセージを受信し続け、応答とイベントを振り分ける (daemon スレッド)。"""
        connection = self._ws
        if connection is None:
            return
        while self._running:
            try:
                text = connection.recv_text(timeout=0.5)
            except Exception:
                break
            if text is None:
                # ソケットが閉じられたらループを抜ける (開いている間は受信タイムアウト待ち)
                if connection.closed:
                    break
                continue
            try:
                message = json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            if 'id' in message:
                # id 付きはコマンド応答
                self._resolve_pending(message)
            elif 'method' in message:
                # method 付きはイベント
                try:
                    self._handle_event(message)
                except Exception:
                    # イベント処理の例外で受信ループを止めない
                    continue

        # 接続断でループを抜けた場合、keep_open=False なら後始末する (受信スレッドから直接
        # stop() を呼ぶと自分自身の join で待ち続けるため、別スレッドに任せる)
        if self._running and not self._keep_open:
            self._running = False
            threading.Thread(target=self.stop, name='m3u8dl-cdp-cleanup', daemon=True).start()

    def _resolve_pending(self, message: dict) -> None:
        """受信した応答メッセージを待機中のスロットに引き渡す。

        Args:
            message: 'id' を持つ CDP 応答メッセージ。
        """
        command_id = message.get('id')
        with self._pending_lock:
            slot = self._pending.pop(command_id, None)
        if slot is None:
            return
        if 'error' in message:
            slot['error'] = message['error']
        else:
            slot['result'] = message.get('result')
        event = slot['event']
        if isinstance(event, threading.Event):
            event.set()

    def _handle_event(self, message: dict) -> None:
        """CDP イベントを解析して検出結果をコールバックへ流す。

        Args:
            message: 'method' を持つ CDP イベントメッセージ。
        """
        method = message.get('method')
        params = message.get('params')
        if not isinstance(params, dict):
            params = {}

        if method == 'Network.requestWillBeSent':
            request = params.get('request')
            if not isinstance(request, dict):
                return
            url = request.get('url')
            if not isinstance(url, str) or not url:
                return
            request_id = params.get('requestId')
            sniffed = SniffedRequest(
                url=url,
                kind=_classify(url),
                resource_type=str(params.get('type', 'Other')),
                method=str(request.get('method', 'GET')),
                status=None,
            )
            if isinstance(request_id, str) and request_id:
                with self._requests_lock:
                    self._requests[request_id] = sniffed
            self._deliver(sniffed)

        elif method == 'Network.responseReceived':
            response = params.get('response')
            if not isinstance(response, dict):
                return
            url = response.get('url')
            if not isinstance(url, str) or not url:
                return
            kind = _classify(url)
            # ステータスが有用なプレイリストだけ、応答時に改めて通知する
            if kind != 'playlist':
                return
            status = response.get('status')
            request_id = params.get('requestId')
            method_name = 'GET'
            if isinstance(request_id, str):
                with self._requests_lock:
                    known = self._requests.get(request_id)
                if known is not None:
                    method_name = known.method
            self._deliver(
                SniffedRequest(
                    url=url,
                    kind=kind,
                    resource_type=str(params.get('type', 'Other')),
                    method=method_name,
                    status=int(status) if isinstance(status, int) else None,
                )
            )

    def _deliver(self, request: SniffedRequest) -> None:
        """検出結果を on_request コールバックへ渡す (コールバックの例外は握りつぶす)。

        Args:
            request: 検出したリクエスト。
        """
        callback = self._on_request
        if callback is None:
            return
        try:
            callback(request)
        except Exception:
            # GUI 側のコールバックで例外が起きても受信ループは止めない
            pass

    def _emit_status(self, message: str) -> None:
        """状態メッセージを on_status コールバックへ渡す (例外は握りつぶす)。

        Args:
            message: 状態メッセージ。
        """
        callback = self._on_status
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            pass

    def _terminate_process(self, proc: subprocess.Popen[bytes]) -> None:
        """ブラウザプロセスとその子プロセスを終了する。

        Args:
            proc: 起動したブラウザプロセス。
        """
        try:
            if os.name == 'nt':
                # 子プロセス (レンダラー・GPU など) ごと確実に終了させる
                subprocess.run(
                    ['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass

    def _cleanup_profile(self) -> None:
        """自前で作った一時プロファイルディレクトリを削除する (best-effort)。

        Chrome の子プロセスがファイルハンドルを離すまでに少し時間がかかるため、
        削除に失敗したら短い間隔で数回リトライする。
        """
        if not self._temp_profile:
            return
        path = self._temp_profile
        self._temp_profile = None
        for _ in range(5):
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                return
            time.sleep(0.3)
