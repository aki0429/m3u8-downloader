"""m3u8 (HLS) ダウンローダーの GUI。

ttk による単一ファイルのデスクトップアプリで、CDP でブラウザを解析して m3u8 を検出し、
Cookie とヘッダを付けて保存する。GUI を表示せずにモジュール結合を検証する
run_selftest() も併せて提供する (exe ビルドの動作確認に使う)。

並列実装中の兄弟モジュール (downloader / browser / cookies) が未作成でも起動が
止まらないよう、import はすべて try/except で包み、不足分は missing_modules として
記録して GUI 起動時に警告する。
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from m3u8dl import __version__
from m3u8dl.crypto_aes import backend_name as _aes_backend_name
from m3u8dl.errors import DownloadCancelled
from m3u8dl.hls import MediaPlaylist, Variant, format_duration, parse_playlist

# ---------------------------------------------------------------------------
# tkinter の読み込み
# tkinter が無い環境でも run_selftest() が「利用可否」を報告できるよう、
# import 自体を握りつぶしてフラグで管理する。
# ---------------------------------------------------------------------------
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _TKINTER_AVAILABLE = True
    _TKINTER_IMPORT_ERROR = ''
except Exception as exc:  # pragma: no cover - 環境依存
    tk = None  # type: ignore[assignment]
    filedialog = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _TKINTER_AVAILABLE = False
    _TKINTER_IMPORT_ERROR = str(exc)

# ---------------------------------------------------------------------------
# 兄弟モジュールの読み込み
# 担当が並列で実装中のため、未作成でも import で落ちないようにする。
# 欠落したモジュール名は _MISSING_MODULES にためて GUI 起動時と run_selftest で通知する。
# ---------------------------------------------------------------------------
_MISSING_MODULES: list[str] = []


def _record_missing(name: str) -> None:
    """不足している兄弟モジュール名を記録する。

    Args:
        name: モジュールの短い名前 ('downloader' など)。
    """
    if name not in _MISSING_MODULES:
        _MISSING_MODULES.append(name)


try:
    from m3u8dl.downloader import DownloadOptions, HLSDownloader

    _DOWNLOADER_AVAILABLE = True
except Exception:
    DownloadOptions = None  # type: ignore[assignment]
    HLSDownloader = None  # type: ignore[assignment]
    _DOWNLOADER_AVAILABLE = False
    _record_missing('downloader')

try:
    from m3u8dl.browser import BrowserSniffer

    _BROWSER_AVAILABLE = True
except Exception:
    BrowserSniffer = None  # type: ignore[assignment]
    _BROWSER_AVAILABLE = False
    _record_missing('browser')

try:
    from m3u8dl.cookies import (
        CookieEntry,
        from_cdp_cookies,
        load_file,
        parse_cookie_text,
        save_netscape,
        to_cookie_header,
    )

    _COOKIES_AVAILABLE = True
except Exception:
    CookieEntry = None  # type: ignore[assignment]
    from_cdp_cookies = None  # type: ignore[assignment]
    load_file = None  # type: ignore[assignment]
    parse_cookie_text = None  # type: ignore[assignment]
    save_netscape = None  # type: ignore[assignment]
    to_cookie_header = None  # type: ignore[assignment]
    _COOKIES_AVAILABLE = False
    _record_missing('cookies')


# 画質選択の選択肢。ダウンローダー側の mode 文字列 ('best' / 'worst' / 'manual') に対応する。
_QUALITY_BEST = '自動(最高)'
_QUALITY_WORST = '自動(最低)'
_QUALITY_MANUAL = '手動選択'
_QUALITY_CHOICES = (_QUALITY_BEST, _QUALITY_WORST, _QUALITY_MANUAL)

# SniffedRequest.kind を日本語表示に変換するテーブル
_KIND_LABELS = {
    'playlist': 'プレイリスト',
    'segment': 'セグメント',
    'key': '暗号鍵',
    'other': 'その他',
}

# ブラウザ解析時に付与する既定の User-Agent (サイト側の簡易ブロックを避ける)
_DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# 既定の保存先ファイル名を生成するためのタイムスタンプ書式
_TIMESTAMP_FORMAT = '%Y%m%d_%H%M%S'


def _default_output_path() -> Path:
    """既定の保存先パスを生成する。

    Returns:
        '~/Downloads/m3u8_<日時>.mp4' 形式の絶対パス。
    """
    downloads_dir = Path.home() / 'Downloads'
    stamp = datetime.now().strftime(_TIMESTAMP_FORMAT)
    return downloads_dir / f'm3u8_{stamp}.mp4'


def _tk_usable() -> bool:
    """tkinter で実際にウィンドウを作成できるかを確認する。

    モジュールが import できても表示環境が無いことがあるため、実際に Tk ルートを
    作成→破棄して判定する。selftest から呼ばれるのでウィンドウは withdraw で隠す。

    Returns:
        作成に成功したら True。
    """
    if not _TKINTER_AVAILABLE:
        return False
    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.update_idletasks()
        probe.destroy()
        return True
    except Exception:  # pragma: no cover - 環境依存
        return False


class DownloaderApp:
    """m3u8 ダウンローダーのメインウィンドウ。

    ウィジェットの構築・ブラウザ解析の制御・ダウンロードの起動と進捗表示を担う。
    """

    def __init__(self, root: 'tk.Tk') -> None:
        """アプリケーションを初期化してウィジェットを構築する。

        Args:
            root: tkinter のルートウィンドウ。
        """
        self.root = root

        # --- ウィジェットの状態を保持する tkinter 変数群 ---
        # url_var: 解析・ダウンロード対象の URL
        self.url_var = tk.StringVar()
        # cookie_use_var: Cookie を HTTP ヘッダに付与するか
        self.cookie_use_var = tk.BooleanVar(value=True)
        # quality_var: 画質選択 (自動/手動) の表示値
        self.quality_var = tk.StringVar(value=_QUALITY_BEST)
        # concurrency_var: セグメントの同時接続数
        self.concurrency_var = tk.IntVar(value=8)
        # convert_var: ffmpeg で mp4 に変換するか
        self.convert_var = tk.BooleanVar(value=True)
        # live_var: ライブ配信を収録し続けるか
        self.live_var = tk.BooleanVar(value=False)
        # max_segments_var: 最大セグメント数 (空文字は無制限)
        self.max_segments_var = tk.StringVar(value='')
        # show_browser_var: 解析時にブラウザの画面を表示するか。
        # 既定は False = 画面を出さずにバックグラウンドで解析する (利用者の作業を邪魔しない)。
        # 画面を出せば、ログインや年齢確認などページ側の操作が必要なサイトでも手動で対応できる。
        self.show_browser_var = tk.BooleanVar(value=False)
        # output_var: 保存先ファイルパス
        self.output_var = tk.StringVar(value=str(_default_output_path()))

        # --- 実行時状態 ---
        # ffmpeg_path: 見つかった ffmpeg のパス (無ければ空文字)。mp4 変換の可否を左右する。
        self.ffmpeg_path: str = shutil.which('ffmpeg') or ''
        # variants: 直近に解析したマスタープレイリストのバリアント一覧 (画質 Combobox に反映)
        self.variants: list[Variant] = []
        # sniffer: 稼働中の BrowserSniffer (未起動なら None)
        self.sniffer: Any = None
        # downloader: 稼働中の HLSDownloader (未起動なら None)
        self.downloader: Any = None
        # busy: 解析またはダウンロードの実行中かどうか (二重実行ガード)
        self.busy = False
        # page_url: 解析を開始したページ URL (Referer に使う)
        self.page_url: str = ''
        # probed_playlists: 既にプレイリスト種別を判定済みの URL (重複取得を防ぐ)
        self.probed_playlists: set[str] = set()
        # tree_items: URL から Treeview のアイテム ID への対応 (重複行を防ぐ)
        self.tree_items: dict[str, str] = {}
        # lockable_widgets: 実行中に無効化する入出力ウィジェット
        self.lockable_widgets: list[Any] = []

        self._configure_style()
        self._build_widgets()
        self._report_missing_modules()

    # ------------------------------------------------------------------
    # ウィジェット構築
    # ------------------------------------------------------------------
    def _configure_style(self) -> None:
        """ttk のスタイルを最小限に整える (既定テーマのまま崩れないようにする)。"""
        style = ttk.Style()
        # 利用可能なら Windows ネイティブ風の 'vista' を使う
        if 'vista' in style.theme_names():
            style.theme_use('vista')

    def _build_widgets(self) -> None:
        """画面全体のウィジェットを構築する。"""
        self.root.title(f'm3u8 ダウンローダー {__version__}')
        self.root.minsize(900, 700)

        # 0) ルートのグリッド設定 (ログ欄が余白を吸収するようにする)
        self.root.rowconfigure(0, weight=0)  # URL 入力
        self.root.rowconfigure(1, weight=1)  # 検出結果一覧
        self.root.rowconfigure(2, weight=0)  # Cookie
        self.root.rowconfigure(3, weight=0)  # オプション
        self.root.rowconfigure(4, weight=1)  # 進捗 + ログ
        self.root.columnconfigure(0, weight=1)

        self._build_url_row()
        self._build_result_tree()
        self._build_cookie_frame()
        self._build_options_frame()
        self._build_progress_frame()

    def _build_url_row(self) -> None:
        """上段の URL 入力欄と解析ボタンを構築する。"""
        frame = ttk.LabelFrame(self.root, text='URL')
        frame.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 4))
        frame.columnconfigure(0, weight=1)

        self.url_entry = ttk.Entry(frame, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=0, sticky='ew', padx=(8, 4), pady=6)

        self.analyze_button = ttk.Button(frame, text='ブラウザで解析', command=self._on_analyze)
        self.analyze_button.grid(row=0, column=1, padx=4, pady=6)

        self.stop_analyze_button = ttk.Button(frame, text='解析停止', command=self._on_stop_analysis)
        self.stop_analyze_button.grid(row=0, column=2, padx=(4, 8), pady=6)

        # ブラウザを起動できない環境向け: ページの内容から m3u8 の URL を直接探す
        self.scan_button = ttk.Button(frame, text='URL を抽出', command=self._on_scan_page)
        self.scan_button.grid(row=0, column=3, padx=(0, 8), pady=6)

        self.lockable_widgets += [self.url_entry, self.analyze_button, self.stop_analyze_button, self.scan_button]

    def _build_result_tree(self) -> None:
        """検出結果を表示する Treeview を構築する。"""
        frame = ttk.LabelFrame(self.root, text='検出結果 (ダブルクリックで URL 欄へ転記)')
        frame.grid(row=1, column=0, sticky='nsew', padx=8, pady=4)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # 列: 種類 / URL / ステータス
        columns = ('kind', 'url', 'status')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', height=7)
        self.tree.heading('kind', text='種類')
        self.tree.heading('url', text='URL')
        self.tree.heading('status', text='ステータス')
        self.tree.column('kind', width=110, stretch=False, anchor='center')
        self.tree.column('url', width=640, stretch=True, anchor='w')
        self.tree.column('status', width=90, stretch=False, anchor='center')
        self.tree.grid(row=0, column=0, sticky='nsew', padx=(8, 0), pady=6)

        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns', padx=(0, 8), pady=6)
        self.tree.configure(yscrollcommand=scrollbar.set)

        # ダブルクリックで URL を URL 欄へ転記する
        self.tree.bind('<Double-1>', self._on_tree_double_click)

    def _build_cookie_frame(self) -> None:
        """Cookie 入力欄と関連ボタンを構築する。"""
        frame = ttk.LabelFrame(self.root, text='Cookie')
        frame.grid(row=2, column=0, sticky='ew', padx=8, pady=4)
        frame.columnconfigure(0, weight=1)

        # 複数行の Cookie 入力 ('a=1; b=2' 形式 / Netscape 形式をそのまま貼り付けてよい)
        self.cookie_text = tk.Text(frame, height=4, wrap='none')
        self.cookie_text.grid(row=0, column=0, rowspan=3, sticky='ew', padx=(8, 4), pady=6)

        cookie_scroll = ttk.Scrollbar(frame, orient='vertical', command=self.cookie_text.yview)
        cookie_scroll.grid(row=0, column=1, rowspan=3, sticky='ns', pady=6)
        self.cookie_text.configure(yscrollcommand=cookie_scroll.set)

        self.cookie_use_check = ttk.Checkbutton(frame, text='Cookie を使う', variable=self.cookie_use_var)
        self.cookie_use_check.grid(row=0, column=2, sticky='w', padx=4, pady=(6, 2))

        self.cookie_browser_button = ttk.Button(
            frame, text='ブラウザからCookie取得', command=self._on_get_browser_cookies
        )
        self.cookie_browser_button.grid(row=1, column=2, sticky='ew', padx=4, pady=2)

        self.cookie_file_button = ttk.Button(frame, text='ファイル読込', command=self._on_load_cookie_file)
        self.cookie_file_button.grid(row=2, column=2, sticky='ew', padx=4, pady=(2, 6))

        self.lockable_widgets += [
            self.cookie_text,
            self.cookie_use_check,
            self.cookie_browser_button,
            self.cookie_file_button,
        ]

    def _build_options_frame(self) -> None:
        """保存先とダウンロードオプションを構築する。"""
        frame = ttk.LabelFrame(self.root, text='保存先とオプション')
        frame.grid(row=3, column=0, sticky='ew', padx=8, pady=4)
        frame.columnconfigure(1, weight=1)

        # 保存先ファイルパス
        ttk.Label(frame, text='保存先:').grid(row=0, column=0, sticky='w', padx=(8, 4), pady=6)
        self.output_entry = ttk.Entry(frame, textvariable=self.output_var)
        self.output_entry.grid(row=0, column=1, sticky='ew', padx=4, pady=6)
        self.browse_button = ttk.Button(frame, text='参照', command=self._on_browse, width=6)
        self.browse_button.grid(row=0, column=2, sticky='w', padx=(4, 8), pady=6)

        # 画質選択
        ttk.Label(frame, text='画質:').grid(row=1, column=0, sticky='w', padx=(8, 4), pady=6)
        self.quality_combo = ttk.Combobox(
            frame, textvariable=self.quality_var, values=list(_QUALITY_CHOICES), state='readonly', width=14
        )
        self.quality_combo.grid(row=1, column=1, sticky='w', padx=4, pady=6)
        self.quality_combo.bind('<<ComboboxSelected>>', self._on_quality_changed)

        # バリアント (解像度) 選択。マスタープレイリスト検出時に label を並べる。
        ttk.Label(frame, text='解像度:').grid(row=2, column=0, sticky='w', padx=(8, 4), pady=6)
        self.variant_combo = ttk.Combobox(frame, state='disabled', width=40)
        self.variant_combo.grid(row=2, column=1, sticky='w', padx=4, pady=6)

        # 同時接続数
        ttk.Label(frame, text='同時接続数:').grid(row=1, column=2, sticky='w', padx=(8, 4), pady=6)
        self.concurrency_spin = ttk.Spinbox(frame, from_=1, to=64, textvariable=self.concurrency_var, width=6)
        self.concurrency_spin.grid(row=1, column=3, sticky='w', padx=(4, 8), pady=6)

        # ffmpeg で mp4 に変換 (ffmpeg が無いときは無効化する)
        self.convert_check = ttk.Checkbutton(frame, text='ffmpeg で mp4 に変換', variable=self.convert_var)
        self.convert_check.grid(row=2, column=2, columnspan=2, sticky='w', padx=4, pady=6)

        # ライブ収録
        self.live_check = ttk.Checkbutton(frame, text='ライブ収録', variable=self.live_var)
        self.live_check.grid(row=3, column=0, columnspan=2, sticky='w', padx=8, pady=6)

        # 最大セグメント数
        ttk.Label(frame, text='最大セグメント数:').grid(row=3, column=2, sticky='w', padx=(8, 4), pady=6)
        self.max_segments_entry = ttk.Entry(frame, textvariable=self.max_segments_var, width=8)
        self.max_segments_entry.grid(row=3, column=3, sticky='w', padx=(4, 8), pady=6)

        # 解析時にブラウザの画面を表示するか (既定はバックグラウンド = 画面を出さない)
        self.show_browser_check = ttk.Checkbutton(
            frame, text='解析中にブラウザの画面を表示 (通常はバックグラウンドで解析します)', variable=self.show_browser_var
        )
        self.show_browser_check.grid(row=4, column=0, columnspan=4, sticky='w', padx=8, pady=6)

        self.lockable_widgets += [
            self.output_entry,
            self.browse_button,
            self.quality_combo,
            self.variant_combo,
            self.concurrency_spin,
            self.convert_check,
            self.live_check,
            self.max_segments_entry,
            self.show_browser_check,
        ]

        # ffmpeg が無ければ mp4 変換を無効化し、ログで警告する
        if not self.ffmpeg_path:
            self.convert_var.set(False)
            self.convert_check.state(['disabled'])

    def _build_progress_frame(self) -> None:
        """進捗バー・ステータスラベル・ログ欄・開始/キャンセルボタンを構築する。"""
        frame = ttk.LabelFrame(self.root, text='進捗とログ')
        frame.grid(row=4, column=0, sticky='nsew', padx=8, pady=(4, 8))
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(frame, mode='determinate', maximum=100, value=0)
        self.progress.grid(row=0, column=0, sticky='ew', padx=8, pady=(6, 2))

        self.status_label = ttk.Label(frame, text='待機中')
        self.status_label.grid(row=1, column=0, sticky='w', padx=8, pady=(0, 4))

        # 読み取り専用のログ欄。tk.Text は state を切り替えて書き込む。
        self.log_text = tk.Text(frame, height=10, wrap='word', state='disabled')
        self.log_text.grid(row=2, column=0, sticky='nsew', padx=(8, 0), pady=(0, 4))
        log_scroll = ttk.Scrollbar(frame, orient='vertical', command=self.log_text.yview)
        log_scroll.grid(row=2, column=1, sticky='ns', padx=(0, 8), pady=(0, 4))
        self.log_text.configure(yscrollcommand=log_scroll.set)

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=2, sticky='ew', padx=8, pady=(0, 8))
        button_row.columnconfigure(0, weight=1)

        self.start_button = ttk.Button(button_row, text='ダウンロード開始', command=self._on_start_download)
        self.start_button.grid(row=0, column=0, sticky='w')

        self.cancel_button = ttk.Button(button_row, text='キャンセル', command=self._on_cancel)
        self.cancel_button.grid(row=0, column=1, sticky='e')
        # キャンセルは実行中のみ有効にする
        self.cancel_button.state(['disabled'])

        self.lockable_widgets.append(self.start_button)

    def _report_missing_modules(self) -> None:
        """不足している兄弟モジュールがあれば起動時に警告する。"""
        if not _MISSING_MODULES:
            return
        names = ', '.join(_MISSING_MODULES)
        self._append_log(f'警告: 次のモジュールが見つかりません: {names}')
        self._append_log('一部の機能 (ブラウザ解析 / Cookie / ダウンロード) は利用できません。')
        # ウィンドウ表示後に警告ダイアログを出す (起動直後の描画を邪魔しないよう after を使う)
        self.root.after(
            200,
            lambda: messagebox.showwarning(
                'モジュール不足',
                f'次のモジュールが見つかりません: {names}\n'
                '該当機能は利用できません。実装が完了してから再起動してください。',
            ),
        )

    # ------------------------------------------------------------------
    # ログ
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        """ログメッセージをメインスレッドで追記する (ワーカースレッドから呼んでよい)。

        Args:
            message: 表示するメッセージ。
        """
        self.root.after(0, self._append_log, message)

    def _append_log(self, message: str) -> None:
        """日時付きでログ欄にメッセージを追記する (メインスレッド専用)。

        Args:
            message: 表示するメッセージ。
        """
        stamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.configure(state='normal')
        self.log_text.insert('end', f'[{stamp}] {message}\n')
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    # ------------------------------------------------------------------
    # ヘッダ / Cookie
    # ------------------------------------------------------------------
    def _build_headers(self) -> dict[str, str]:
        """HTTP リクエストに付与するヘッダを組み立てる (メインスレッド専用)。

        tkinter 変数を読むためメインスレッドから呼ぶこと。ワーカーにはこの戻り値を
        スナップショットとして渡す。

        Returns:
            ヘッダ名をキーとする辞書。Cookie が不要なら 'Cookie' を含まない。
        """
        headers: dict[str, str] = {'User-Agent': _DEFAULT_USER_AGENT}
        if self.page_url:
            headers['Referer'] = self.page_url
        cookie_header = self._cookie_header()
        if cookie_header:
            headers['Cookie'] = cookie_header
        return headers

    def _cookie_header(self) -> str:
        """Cookie 入力欄の内容を HTTP ヘッダ用の文字列に変換する (メインスレッド専用)。

        Returns:
            'a=1; b=2' 形式の文字列。無効時や空欄時は空文字。cookies モジュールが
            無い場合も空文字を返し、機能を無効化する。
        """
        if not self.cookie_use_var.get() or not _COOKIES_AVAILABLE:
            return ''
        text = self.cookie_text.get('1.0', 'end').strip()
        if not text:
            return ''
        try:
            entries = parse_cookie_text(text)
            return to_cookie_header(entries)
        except Exception as exc:
            self._append_log(f'Cookie の解析に失敗しました: {exc}')
            return ''

    # ------------------------------------------------------------------
    # 状態切り替え
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        """実行中フラグに応じて入出力を無効化/復帰させる。

        Args:
            busy: True で実行中 (入力を無効化)、False で待機中 (入力を復帰)。
        """
        self.busy = busy
        for widget in self.lockable_widgets:
            try:
                widget.configure(state='disabled' if busy else 'normal')
            except Exception:
                # 一部の ttk ウィジェットは state 指定を受け付けないことがあるため無視する
                pass
        # readonly の Combobox は 'normal' に戻すと編集可能になってしまうため個別に戻す
        if not busy and self.quality_combo is not None:
            self.quality_combo.configure(state='readonly')
        if not busy and self.convert_check is not None and not self.ffmpeg_path:
            self.convert_check.state(['disabled'])
        # キャンセルボタンは busy のときだけ有効にする
        try:
            self.cancel_button.configure(state='normal' if busy else 'disabled')
        except Exception:
            pass

    # ------------------------------------------------------------------
    # ブラウザ解析
    # ------------------------------------------------------------------
    def _on_analyze(self) -> None:
        """「ブラウザで解析」ボタンのハンドラ。"""
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning('URL 未入力', '解析するページの URL を入力してください。')
            return
        if not _BROWSER_AVAILABLE:
            messagebox.showerror('機能不可', 'm3u8dl.browser が読み込めないため解析できません。')
            return

        self.page_url = url
        # ヘッダはメインスレッドで確定させ、ワーカーへスナップショットを渡す
        headers = self._build_headers()
        # 画面を出すかどうかもここ (メインスレッド) で確定させる。
        # 既定は False = バックグラウンドで解析し、利用者の作業を邪魔しない。
        show_window = bool(self.show_browser_var.get())
        self._set_busy(True)
        mode = '画面あり' if show_window else 'バックグラウンド'
        self._append_log(f'ブラウザ解析を開始します ({mode}): {url}')
        threading.Thread(target=self._run_analysis, args=(url, headers, show_window), daemon=True).start()

    def _run_analysis(self, page_url: str, headers: dict[str, str], show_window: bool = False) -> None:
        """ブラウザを起動して CDP 解析を開始する (ワーカースレッド)。

        Args:
            page_url: 解析対象のページ URL。
            headers: プレイリスト取得に使うヘッダのスナップショット。
            show_window: True ならブラウザの画面を表示する (False ならバックグラウンドで解析する)。
        """
        try:
            sniffer = BrowserSniffer(  # type: ignore[misc]
                on_request=lambda request: self.root.after(0, self._on_sniffed_request, request, headers),
                on_status=lambda message: self.log(message),
                # 画面を表示しない場合はヘッドレスで動かす (バックグラウンドで解析するため)
                headless=not show_window,
            )
            self.sniffer = sniffer
            sniffer.start(page_url)
            if show_window:
                self.log('ブラウザ解析を実行中です。ページを操作して動画を再生してください。')
            else:
                self.log('バックグラウンドでブラウザを動かし、ページの再生を自動で操作して取得内容を解析しています...')
        except Exception as exc:
            # ブラウザを起動できない環境では、ページの内容から URL を直接探して最低限の結果を返す
            self.log('ブラウザで解析できないため、ページの内容から URL を直接探します...')
            self._scan_fallback(page_url, headers)
            self.root.after(0, self._handle_exception, 'ブラウザ解析', exc)

    def _on_scan_page(self) -> None:
        """「URL を抽出」ボタンのハンドラ (ブラウザを使わずに URL を探す)。"""
        url = self.url_var.get().strip()
        if not url:
            self._append_log('URL を入力してください。')
            return
        headers = self._build_headers()
        # 連打でスレッドが増えないように、探索の間はボタンを無効化する
        self.scan_button.configure(state='disabled')
        threading.Thread(target=self._run_scan, args=(url, headers), daemon=True).start()

    def _run_scan(self, page_url: str, headers: dict[str, str]) -> None:
        """ページの内容から m3u8 の URL を探す (ワーカースレッド)。

        Args:
            page_url: 探索するページの URL。
            headers: 取得に使うヘッダのスナップショット。
        """
        try:
            self.log(f'ブラウザを使わずに URL を探します: {page_url}')
            self._scan_fallback(page_url, headers)
        finally:
            self.root.after(0, lambda: self.scan_button.configure(state='normal'))

    def _scan_fallback(self, page_url: str, headers: dict[str, str]) -> None:
        """ページとその JavaScript から m3u8 の URL を探して一覧に追加する。

        ブラウザを起動できない環境 (リモートデバッグの禁止・管理者権限での起動の影響・
        セキュリティ製品など) でも結果を出せるようにするためのフォールバック。

        Args:
            page_url: 探索するページの URL。
            headers: 取得に使うヘッダのスナップショット。
        """
        from m3u8dl.scan import scan_page_for_playlists

        try:
            result = scan_page_for_playlists(page_url, headers=headers)
        except Exception as exc:
            self.log(f'ページの内容からの探索に失敗しました: {exc}')
            return
        for error in result.errors[:3]:
            self.log(f'取得に失敗しました: {error}')
        if not result.playlists:
            self.log('ページの内容からは URL を見つけられませんでした (JavaScript が URL を組み立てるサイトでは見つかりません)。')
            return
        for playlist in result.playlists:
            self.log(f'ページの内容から見つけました: {playlist}')
            self.root.after(0, self._register_playlist, playlist, headers)

    def _register_playlist(self, url: str, headers: dict[str, str]) -> None:
        """見つけたプレイリストを一覧へ追加し、種別の判定を始める (メインスレッド)。

        Args:
            url: プレイリスト URL。
            headers: プレイリスト取得に使うヘッダのスナップショット。
        """
        self._add_tree_row('playlist', url, None)
        if url not in self.probed_playlists:
            self.probed_playlists.add(url)
            threading.Thread(target=self._probe_playlist, args=(url, headers), daemon=True).start()

    def _on_stop_analysis(self) -> None:
        """「解析停止」ボタンのハンドラ。"""
        if self.sniffer is None:
            self._append_log('解析は実行されていません。')
            return
        try:
            self.sniffer.stop()
            self._append_log('ブラウザ解析を停止しました。')
        except Exception as exc:
            self._handle_exception('解析停止', exc)
        finally:
            self.sniffer = None
            self._set_busy(False)

    def _on_sniffed_request(self, request: Any, headers: dict[str, str]) -> None:
        """検出したリクエストを一覧に追加する (メインスレッド)。

        Args:
            request: SniffedRequest (url / kind / resource_type / method / status)。
            headers: プレイリスト取得に使うヘッダのスナップショット。
        """
        url = getattr(request, 'url', '')
        kind = getattr(request, 'kind', 'other')
        status = getattr(request, 'status', None)
        if not url:
            return
        self._add_tree_row(kind, url, status)
        # プレイリストは種別を判定してバリアントを列挙する (重複取得は避ける)
        if kind == 'playlist' and url not in self.probed_playlists:
            self.probed_playlists.add(url)
            threading.Thread(target=self._probe_playlist, args=(url, headers), daemon=True).start()

    def _add_tree_row(self, kind: str, url: str, status: Any) -> None:
        """Treeview に 1 行追加する。既出 URL は重複させない。

        Args:
            kind: 'playlist' / 'segment' / 'key' / 'other'。
            url: リクエスト URL。
            status: HTTP ステータス (None 可)。
        """
        if url in self.tree_items:
            return
        label = _KIND_LABELS.get(kind, kind)
        status_text = '' if status is None else str(status)
        item = self.tree.insert('', 'end', values=(label, url, status_text))
        self.tree_items[url] = item

    def _on_tree_double_click(self, event: Any) -> None:
        """一覧のダブルクリックで URL を URL 欄へ転記する。

        Args:
            event: tkinter のイベント (未使用)。
        """
        del event
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], 'values')
        if len(values) >= 2:
            self.url_var.set(values[1])
            self._append_log(f'URL 欄へ転記しました: {values[1]}')

    def _probe_playlist(self, url: str, headers: dict[str, str]) -> None:
        """プレイリストを取得して種別を判定する (ワーカースレッド)。

        Args:
            url: 判定対象のプレイリスト URL。
            headers: 取得に使うヘッダのスナップショット。
        """
        try:
            text = self._fetch_text(url, headers)
            kind, parsed = parse_playlist(text, url)
            self.root.after(0, self._apply_probe, url, kind, parsed)
        except Exception as exc:
            self.log(f'プレイリストの解析に失敗しました ({url}): {exc}')

    def _apply_probe(self, url: str, kind: str, parsed: Any) -> None:
        """プレイリスト解析結果を UI に反映する (メインスレッド)。

        Args:
            url: 解析したプレイリスト URL。
            kind: 'master' または 'media'。
            parsed: バリアント一覧 (master) または MediaPlaylist (media)。
        """
        if kind == 'master' and isinstance(parsed, list):
            # マスタープレイリストはバリアントを Combobox に列挙する
            self.variants = parsed
            labels = [variant.label for variant in parsed]
            self.variant_combo.configure(values=labels, state='readonly')
            if labels:
                self.variant_combo.current(0)
            self._append_log(f'マスタープレイリストを検出しました ({len(parsed)} 画質): {url}')
        elif isinstance(parsed, MediaPlaylist):
            # メディアプレイリストはセグメント数と長さを表示する
            duration = format_duration(parsed.total_duration)
            live = 'ライブ' if parsed.is_live else 'VOD'
            self._append_log(f'メディアプレイリストを検出しました ({live}, {len(parsed.segments)} セグメント, {duration}): {url}')

    def _on_quality_changed(self, event: Any) -> None:
        """画質 Combobox の変更時に手動選択の可否を更新する。

        Args:
            event: tkinter のイベント (未使用)。
        """
        del event
        if self.quality_var.get() == _QUALITY_MANUAL and self.variants:
            self.variant_combo.configure(state='readonly')
        elif self.quality_var.get() != _QUALITY_MANUAL:
            self._append_log('画質を自動選択に設定しました。')

    # ------------------------------------------------------------------
    # Cookie 操作
    # ------------------------------------------------------------------
    def _on_get_browser_cookies(self) -> None:
        """「ブラウザからCookie取得」ボタンのハンドラ。"""
        if not _COOKIES_AVAILABLE:
            messagebox.showerror('機能不可', 'm3u8dl.cookies が読み込めないため Cookie を取得できません。')
            return
        if self.sniffer is None or not self.sniffer.is_running():
            messagebox.showwarning('解析未実行', '先に「ブラウザで解析」を実行してください。')
            return
        url = self.url_var.get().strip() or None
        threading.Thread(target=self._fetch_browser_cookies, args=(url,), daemon=True).start()

    def _fetch_browser_cookies(self, url: str | None) -> None:
        """稼働中のブラウザから Cookie を取得して入力欄へ反映する (ワーカースレッド)。

        Args:
            url: 対象 URL (None なら全ドメイン)。
        """
        try:
            raw = self.sniffer.get_cookies(url)
            entries = from_cdp_cookies(raw)  # type: ignore[misc]
            header = to_cookie_header(entries)  # type: ignore[misc]
            self.root.after(0, self._set_cookie_text, header, len(entries))
        except Exception as exc:
            self.root.after(0, self._handle_exception, 'Cookie 取得', exc)

    def _set_cookie_text(self, header: str, count: int) -> None:
        """Cookie 入力欄を取得結果で置き換える (メインスレッド)。

        Args:
            header: HTTP ヘッダ形式の Cookie 文字列。
            count: 取得した Cookie の件数。
        """
        self.cookie_text.delete('1.0', 'end')
        self.cookie_text.insert('1.0', header)
        self._append_log(f'ブラウザから Cookie を {count} 件取得しました。')

    def _on_load_cookie_file(self) -> None:
        """「ファイル読込」ボタンのハンドラ (Netscape / JSON / テキストを自動判別)。"""
        if not _COOKIES_AVAILABLE:
            messagebox.showerror('機能不可', 'm3u8dl.cookies が読み込めないため Cookie を読めません。')
            return
        selected = filedialog.askopenfilename(
            title='Cookie ファイルを選択',
            filetypes=[('Cookie ファイル', '*.txt *.json'), ('すべてのファイル', '*.*')],
        )
        if not selected:
            return
        try:
            entries = load_file(Path(selected))  # type: ignore[misc]
            header = to_cookie_header(entries)  # type: ignore[misc]
            self._set_cookie_text(header, len(entries))
        except Exception as exc:
            self._handle_exception('Cookie ファイル読込', exc)

    # ------------------------------------------------------------------
    # ダウンロード
    # ------------------------------------------------------------------
    def _on_browse(self) -> None:
        """「参照」ボタンのハンドラ。保存先をダイアログで選ぶ。"""
        selected = filedialog.asksaveasfilename(
            title='保存先を選択',
            defaultextension='.mp4',
            initialfile=Path(self.output_var.get()).name or 'output.mp4',
            filetypes=[('MP4', '*.mp4'), ('TS', '*.ts'), ('すべてのファイル', '*.*')],
        )
        if selected:
            self.output_var.set(selected)

    def _on_start_download(self) -> None:
        """「ダウンロード開始」ボタンのハンドラ。"""
        if self.busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning('URL 未入力', 'ダウンロードする m3u8 の URL を入力してください。')
            return
        if not _DOWNLOADER_AVAILABLE:
            messagebox.showerror('機能不可', 'm3u8dl.downloader が読み込めないためダウンロードできません。')
            return

        output_text = self.output_var.get().strip()
        if not output_text:
            messagebox.showwarning('保存先未指定', '保存先のファイルパスを入力してください。')
            return
        output_path = Path(output_text).expanduser()

        # 最大セグメント数の解釈 (空欄は無制限)
        max_segments: int | None = None
        raw_max = self.max_segments_var.get().strip()
        if raw_max:
            try:
                max_segments = int(raw_max)
                if max_segments <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning('入力エラー', '最大セグメント数は正の整数で入力してください。')
                return

        headers = self._build_headers()

        # 画質モードと手動選択インデックスをメインスレッドで確定する
        quality = self.quality_var.get()
        if quality == _QUALITY_MANUAL:
            mode = 'manual'
            index: int | None = self.variant_combo.current() if self.variants else None
            if index is not None and index < 0:
                index = 0
        elif quality == _QUALITY_WORST:
            mode = 'worst'
            index = None
        else:
            mode = 'best'
            index = None

        # ffmpeg が無い状態で mp4 変換は要求できない (チェックは無効化済みだが念のため)
        convert = bool(self.convert_var.get()) and bool(self.ffmpeg_path)
        if self.convert_var.get() and not self.ffmpeg_path:
            self._append_log('ffmpeg が見つからないため .ts で保存します。')

        options = DownloadOptions(  # type: ignore[misc]
            output_path=output_path,
            headers=headers,
            concurrency=int(self.concurrency_var.get() or 8),
            merge_format='mp4' if convert else 'ts',
            ffmpeg_path=self.ffmpeg_path or None,
            max_segments=max_segments,
            live=bool(self.live_var.get()),
        )

        variants_snapshot = list(self.variants)
        self._set_busy(True)
        self.progress.configure(mode='determinate', value=0, maximum=100)
        self.status_label.configure(text='開始しています...')
        self._append_log(f'ダウンロードを開始します: {url} -> {output_path}')
        threading.Thread(
            target=self._run_download,
            args=(url, headers, options, mode, index, variants_snapshot),
            daemon=True,
        ).start()

    def _run_download(
        self,
        url: str,
        headers: dict[str, str],
        options: Any,
        mode: str,
        index: int | None,
        variants: list[Variant],
    ) -> None:
        """ダウンロード本体を実行する (ワーカースレッド)。

        Args:
            url: 入力された URL (マスタープレイリストのこともある)。
            headers: 取得に使うヘッダのスナップショット。
            options: DownloadOptions。
            mode: 画質選択モード ('best' / 'worst' / 'manual')。
            index: 手動選択時のバリアント番号。
            variants: 事前解析済みのバリアント一覧 (空のこともある)。
        """
        try:
            target = self._resolve_target(url, headers, mode, index, variants)
            downloader = HLSDownloader(  # type: ignore[misc]
                options,
                progress_callback=lambda progress: self.root.after(0, self._on_progress, progress),
                log_callback=lambda message: self.log(message),
            )
            self.downloader = downloader
            result_path = downloader.download(target)
            self.root.after(0, self._on_download_success, result_path)
        except DownloadCancelled:
            self.root.after(0, self._on_download_cancelled)
        except Exception as exc:
            self.root.after(0, self._handle_exception, 'ダウンロード', exc)
        finally:
            self.downloader = None

    def _resolve_target(
        self,
        url: str,
        headers: dict[str, str],
        mode: str,
        index: int | None,
        variants: list[Variant],
    ) -> str:
        """ダウンロード対象を解決する (マスタープレイリストなら画質を選択)。

        Args:
            url: 入力 URL。
            headers: 取得に使うヘッダのスナップショット。
            mode: 画質選択モード。
            index: 手動選択時のバリアント番号。
            variants: 事前解析済みのバリアント一覧。

        Returns:
            実際にダウンロードする URL (バリアントメディアプレイリストまたは入力 URL)。
        """
        # 事前解析済みのバリアントがあればそれを使う (再取得しない)
        if variants:
            variant = HLSDownloader.select_variant(variants, mode=mode, index=index)  # type: ignore[misc]
            self.log(f'画質を選択しました: {variant.label}')
            return variant.url
        # 未解析なら取得して種別を判定する
        text = self._fetch_text(url, headers)
        kind, parsed = parse_playlist(text, url)
        if kind == 'master' and isinstance(parsed, list) and parsed:
            variant = HLSDownloader.select_variant(parsed, mode=mode, index=index)  # type: ignore[misc]
            self.log(f'画質を選択しました: {variant.label}')
            return variant.url
        return url

    def _on_progress(self, progress: Any) -> None:
        """ダウンロード進捗を UI に反映する (メインスレッド)。

        Args:
            progress: DownloadProgress。
        """
        total = getattr(progress, 'total_segments', 0) or 0
        done = getattr(progress, 'downloaded_segments', 0) or 0
        if total > 0:
            self.progress.configure(mode='determinate', maximum=total, value=done)
        elif getattr(progress, 'phase', '') == 'done':
            self.progress.configure(value=self.progress.cget('maximum'))
        self.status_label.configure(text=self._format_progress(progress))

    def _format_progress(self, progress: Any) -> str:
        """DownloadProgress をステータスラベル用の文字列に整形する。

        Args:
            progress: DownloadProgress。

        Returns:
            速度・残り時間・進捗を含む表示文字列。
        """
        phase = getattr(progress, 'phase', '')
        message = getattr(progress, 'message', '')
        done = getattr(progress, 'downloaded_segments', 0) or 0
        total = getattr(progress, 'total_segments', 0) or 0
        speed = getattr(progress, 'speed_bps', 0.0) or 0.0
        eta = getattr(progress, 'eta_seconds', None)

        segments = f'{done}/{total}' if total else str(done)
        speed_text = f'{speed / 1024 / 1024:.2f} MiB/s' if speed else '—'
        eta_text = format_duration(eta) if eta is not None else '—'
        return f'[{phase}] {message} | セグメント {segments} | {speed_text} | 残り {eta_text}'

    def _on_download_success(self, result_path: Any) -> None:
        """ダウンロード成功時の後処理 (メインスレッド)。

        Args:
            result_path: 保存されたファイルパス。
        """
        self._append_log(f'ダウンロードが完了しました: {result_path}')
        self.status_label.configure(text=f'完了: {result_path}')
        self.progress.configure(value=self.progress.cget('maximum'))
        self._set_busy(False)

    def _on_download_cancelled(self) -> None:
        """ダウンロード中断時の後処理 (メインスレッド)。"""
        self._append_log('ダウンロードをキャンセルしました。')
        self.status_label.configure(text='キャンセル済み')
        self._set_busy(False)

    def _on_cancel(self) -> None:
        """「キャンセル」ボタンのハンドラ。"""
        if self.downloader is None:
            self._append_log('キャンセルできるダウンロードがありません。')
            return
        try:
            self.downloader.cancel()
            self._append_log('キャンセルを要求しました...')
        except Exception as exc:
            self._handle_exception('キャンセル', exc)

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------
    def _fetch_text(self, url: str, headers: dict[str, str], timeout: float = 20.0) -> str:
        """URL からテキストを取得する (ワーカースレッド専用)。

        Args:
            url: 取得対象 URL。
            headers: HTTP ヘッダ。
            timeout: タイムアウト秒。

        Returns:
            取得したテキスト。

        Raises:
            urllib.error.URLError: 取得に失敗したとき。
        """
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        charset = response.headers.get_content_charset() if hasattr(response, 'headers') else None
        return raw.decode(charset or 'utf-8', errors='replace')

    def _handle_exception(self, context: str, exc: Exception) -> None:
        """例外をログに出しつつエラーダイアログを表示する (メインスレッド専用)。

        Args:
            context: 失敗した処理の名前。
            exc: 発生した例外。
        """
        detail = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append_log(f'{context} でエラーが発生しました: {exc}')
        # スタックトレースはログ欄へ、要点はダイアログで通知する
        self._append_log(detail.rstrip())
        self.status_label.configure(text=f'エラー: {context}')
        self._set_busy(False)
        try:
            messagebox.showerror(f'{context} エラー', f'{context} に失敗しました:\n{exc}')
        except Exception:
            pass

    def on_close(self) -> None:
        """ウィンドウを閉じるときに解析とダウンロードを停止する。"""
        try:
            if self.sniffer is not None:
                self.sniffer.stop()
        except Exception:
            pass
        try:
            if self.downloader is not None:
                self.downloader.cancel()
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------------------
# 自己テスト (GUI を表示しない)
# ---------------------------------------------------------------------------
def _selftest_hls() -> dict[str, Any]:
    """hls パーサの自己テストを行う。

    Returns:
        解析結果の要約 (種別・件数・ラベル)。失敗時は ok=False と error。
    """
    sample = (
        '#EXTM3U\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n'
        'low/index.m3u8\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n'
        'high/index.m3u8\n'
    )
    try:
        kind, parsed = parse_playlist(sample, 'https://example.com/hls/master.m3u8')
        labels = [variant.label for variant in parsed] if isinstance(parsed, list) else []
        count = len(parsed) if isinstance(parsed, list) else len(parsed.segments)
        return {'ok': True, 'kind': kind, 'count': count, 'labels': labels}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def _selftest_cookies() -> dict[str, Any]:
    """cookies パーサの自己テストを行う。

    Returns:
        parse_cookie_text('a=1; b=2') の結果。cookies モジュールが無いときは ok=False。
    """
    if not _COOKIES_AVAILABLE:
        return {'ok': False, 'error': 'm3u8dl.cookies が読み込めません'}
    try:
        entries = parse_cookie_text('a=1; b=2')  # type: ignore[misc]
        return {
            'ok': True,
            'names': [entry.name for entry in entries],
            'values': [entry.value for entry in entries],
            'header': to_cookie_header(entries),  # type: ignore[misc]
        }
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def run_selftest(report_path: Path) -> int:
    """GUI を表示せずにモジュール結合を検証して結果を JSON に書き出す。

    Args:
        report_path: 検証結果を書き出す JSON のパス。

    Returns:
        終了コード。コア検証 (tkinter / hls) が通れば 0、失敗すれば 1。
    """
    report: dict[str, Any] = {}

    # バージョン
    report['version'] = __version__

    # tkinter の利用可否 (import でき、実際にウィンドウを作成できるか)
    report['tkinter'] = _TKINTER_AVAILABLE
    report['tk_usable'] = _tk_usable()
    if not _TKINTER_AVAILABLE:
        report['tkinter_error'] = _TKINTER_IMPORT_ERROR

    # ffmpeg の探索パス (見つからなければ空文字)
    report['ffmpeg_path'] = shutil.which('ffmpeg') or ''

    # AES バックエンド名
    try:
        report['aes_backend'] = _aes_backend_name()
    except Exception as exc:
        report['aes_backend'] = f'error: {exc}'

    # hls パーサの自己テスト
    hls_result = _selftest_hls()
    report['hls_selftest'] = hls_result

    # cookies の自己テスト
    report['cookies_selftest'] = _selftest_cookies()

    # 不足している兄弟モジュール (downloader / browser / cookies)
    report['missing_modules'] = sorted(_MISSING_MODULES)

    # コア検証の成否: tkinter が使え、hls パースが成功すること
    core_ok = bool(report['tkinter']) and bool(hls_result.get('ok'))
    report['ok'] = core_ok

    # JSON を書き出す (日本語ラベルを含むため ensure_ascii=False)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as exc:  # pragma: no cover - ファイルシステム依存
        print(f'自己テスト結果の書き出しに失敗しました: {exc}', file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if core_ok else 1


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------
def main() -> None:
    """tkinter アプリケーションを起動する。"""
    # --selftest <path> が渡されたら GUI を出さずに自己テストだけ実行する
    if '--selftest' in sys.argv:
        index = sys.argv.index('--selftest')
        report_path = Path(sys.argv[index + 1]) if len(sys.argv) > index + 1 else Path('selftest.json')
        sys.exit(run_selftest(report_path))

    if not _TKINTER_AVAILABLE:
        print(f'tkinter が利用できません: {_TKINTER_IMPORT_ERROR}', file=sys.stderr)
        sys.exit(1)

    root = tk.Tk()
    app = DownloaderApp(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    root.mainloop()


if __name__ == '__main__':
    main()
