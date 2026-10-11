"""m3u8dl (m3u8 ダウンローダー GUI) のエントリポイント。

起動方法:
  m3u8dl.exe                                    GUI を起動する
  m3u8dl.exe --selftest <path.json>             画面を出さずに結合検証だけを行う (exe 検証用)
  m3u8dl.exe --sniff <url> [--seconds <n>]      画面を出さずにブラウザ解析だけを行う (検証用)
  m3u8dl.exe --diagnose <path.txt>              ブラウザ解析が始まらない環境の切り分けレポートを書き出す
  m3u8dl.exe --download <url> <out> [options]   画面を出さずにダウンロードする (バッチ / 検証用)
  m3u8dl.exe --version                          バージョンを表示する
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path


def _print_usage() -> None:
    """--download の使い方を標準出力に出す。"""
    print(
        'usage: m3u8dl.exe --download <playlist_url> <output_path> [options]\n'
        '  --cookie <text|file>    Cookie ヘッダ (a=1; b=2) または Netscape 形式のファイル\n'
        '  --referer <url>         Referer ヘッダ\n'
        '  --user-agent <ua>       User-Agent ヘッダ\n'
        '  --concurrency <n>       同時接続数 (既定 8)\n'
        '  --max-segments <n>      最大セグメント数 (部分保存)\n'
        '  --ts                    mp4 に変換せず .ts で保存する\n'
        '  --live                  ライブ配信を収録する (ENDLIST まで追記)\n',
    )


def _build_cli_headers(arguments: dict[str, str]) -> dict[str, str]:
    """CLI 引数から HTTP ヘッダを組み立てる。

    Args:
        arguments: オプション名をキー、値を値とする辞書。

    Returns:
        HTTP ヘッダの辞書。Cookie は 'Cookie' ヘッダにまとめる。
    """
    from m3u8dl.cookies import load_file, parse_cookie_text, to_cookie_header

    # ブラウザからの要求に近づけるため、既定の UA を入れておく (指定があれば上書き)
    headers: dict[str, str] = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
        ),
        'Accept': '*/*',
        'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    }
    if '--user-agent' in arguments:
        headers['User-Agent'] = arguments['--user-agent']
    if '--referer' in arguments:
        headers['Referer'] = arguments['--referer']

    # Cookie は「既存ファイルのパスなら読み込み、それ以外は文字列として解釈」する
    cookie_argument = arguments.get('--cookie', '')
    if cookie_argument:
        cookie_path = Path(cookie_argument)
        if cookie_path.exists() and cookie_path.is_file():
            entries = load_file(cookie_path)
        else:
            entries = parse_cookie_text(cookie_argument)
        cookie_header = to_cookie_header(entries, arguments.get('_url', ''))
        if cookie_header:
            headers['Cookie'] = cookie_header
    return headers


def _run_cli_download(argv: list[str]) -> int:
    """画面を出さずにダウンロードを実行する。

    Args:
        argv: `--download` 以降を含むコマンドライン引数。

    Returns:
        終了コード (0=成功, 1=失敗, 2=引数エラー)。
    """
    from m3u8dl.downloader import DownloadOptions, HLSDownloader
    from m3u8dl.errors import DownloadCancelled, M3U8DLError

    # 位置引数 (URL と出力パス) を先に取り出し、残りを --key value 形式として読む
    positional: list[str] = []
    arguments: dict[str, str] = {}
    index = argv.index('--download') + 1
    while index < len(argv) and len(positional) < 2 and not argv[index].startswith('--'):
        positional.append(argv[index])
        index += 1
    if len(positional) < 2:
        _print_usage()
        return 2
    while index < len(argv):
        name = argv[index]
        if name.startswith('--'):
            # 値を持たないフラグ (--ts / --live) は空文字として記録する
            value = ''
            if index + 1 < len(argv) and not argv[index + 1].startswith('--'):
                value = argv[index + 1]
                index += 1
            arguments[name] = value
        index += 1

    playlist_url, output_text = positional
    output_path = Path(output_text).expanduser()
    arguments['_url'] = playlist_url

    # コンソールが無い環境 (windowed exe) でも結果を確認できるよう、ログをファイルにも出す
    log_path = output_path.with_suffix(output_path.suffix + '.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open('w', encoding='utf-8')

    def log(message: str) -> None:
        """ログを標準出力とログファイルの両方に書く。

        Args:
            message: 出力するメッセージ。
        """
        print(message, flush=True)
        log_file.write(message + '\n')
        log_file.flush()

    log(f'[m3u8dl] playlist: {playlist_url}')
    log(f'[m3u8dl] output  : {output_path}')

    options = DownloadOptions(
        output_path=output_path,
        headers=_build_cli_headers(arguments),
        concurrency=int(arguments.get('--concurrency') or 8),
        merge_format='ts' if '--ts' in arguments else 'mp4',
        ffmpeg_path=None,
        max_segments=int(arguments['--max-segments']) if arguments.get('--max-segments') else None,
        live='--live' in arguments,
    )

    last_line = ''

    def on_progress(progress) -> None:  # type: ignore[no-untyped-def]
        """進捗を 1 行ずつログに残す。

        Args:
            progress: DownloadProgress。
        """
        nonlocal last_line
        total = progress.total_segments or 0
        percent = (progress.downloaded_segments / total * 100) if total else 0.0
        line = (
            f'[{progress.phase}] {percent:5.1f}% '
            f'{progress.downloaded_segments}/{total} seg '
            f'{progress.downloaded_bytes / 1048576:.1f}MB '
            f'{progress.speed_bps / 1048576:.2f}MB/s'
        )
        if line != last_line:
            log(line)
            last_line = line

    try:
        result_path = HLSDownloader(options, progress_callback=on_progress, log_callback=log).download(playlist_url)
    except DownloadCancelled:
        log('[m3u8dl] cancelled')
        log_file.close()
        return 1
    except (M3U8DLError, OSError, ValueError) as exc:
        log(f'[m3u8dl] failed: {exc}')
        log_file.close()
        return 1

    log(f'[m3u8dl] saved: {result_path}')
    log_file.close()
    return 0


# `--browser` で指定できるブラウザ名と実行ファイル名の対応 (パスを直接指定することもできる)
_BROWSER_NAME_MAP = {
    'chrome': ('chrome.exe',),
    'edge': ('msedge.exe',),
    'brave': ('brave.exe',),
}


def _resolve_browser_argument(value: str) -> str | None:
    """`--browser` の値から使用するブラウザの実行ファイルパスを決める。

    ブラウザ名 (chrome / edge / brave、大文字小文字は問わない) または実行ファイルのパスを受け付ける。
    名前で指定された場合は、実際に存在する候補パスから該当するものを探して返す。

    Args:
        value: ブラウザ名または実行ファイルのパス。

    Returns:
        実行ファイルのパス。該当するブラウザが見つからなければ None。
    """
    path = Path(value).expanduser()
    if path.is_file():
        return str(path)
    executable_names = _BROWSER_NAME_MAP.get(value.strip().lower())
    if executable_names is None:
        return None
    # m3u8dl の読み込みはリポジトリルートが sys.path に入った後に行う (ソース実行時のため)
    from m3u8dl.browser import _browser_candidates

    # _browser_candidates() は探索順の候補を並べるだけで存在確認はしないため、ここで実在するものだけを採る
    for candidate in _browser_candidates():
        if os.path.basename(candidate).lower() in executable_names and os.path.isfile(candidate):
            return candidate
    return None


def _run_cli_sniff(argv: list[str]) -> int:
    """画面を出さずにブラウザ解析だけを実行し、検出した URL を JSON に書き出す。

    GUI を開かずに exe のブラウザ解析経路を実測検証するために用意している。
    コンソールが無い windowed exe では標準出力が使えないため、結果は必ずファイルへ出す。

    Args:
        argv: `--sniff` 以降を含むコマンドライン引数。

    Returns:
        終了コード (0=m3u8 を検出, 1=失敗または未検出, 2=引数エラー)。
    """
    import time as time_module

    from m3u8dl.browser import BrowserSniffer
    from m3u8dl.errors import M3U8DLError

    index = argv.index('--sniff') + 1
    if index >= len(argv) or argv[index].startswith('--'):
        print('usage: m3u8dl.exe --sniff <page_url> [--seconds <n>] [--out <json>] [--headed] [--browser <name|path>]')
        return 2
    page_url = argv[index]
    index += 1

    # 残りの引数を --key value 形式で読む (値を持たないフラグは空文字)
    options: dict[str, str] = {}
    while index < len(argv):
        name = argv[index]
        if name.startswith('--'):
            value = ''
            if index + 1 < len(argv) and not argv[index + 1].startswith('--'):
                value = argv[index + 1]
                index += 1
            options[name] = value
        index += 1

    seconds = float(options.get('--seconds') or 25)
    out_path = Path(options.get('--out') or 'sniff.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 使用するブラウザの指定 (省略時は Chrome → Edge → Brave を自動で試す)
    browser_path: str | None = None
    if options.get('--browser'):
        browser_path = _resolve_browser_argument(options['--browser'])
        if browser_path is None:
            print(f'--browser で指定したブラウザが見つかりません: {options["--browser"]}')
            return 2

    # 検出結果を URL で重複排除して蓄積する (同じ URL が何度も来るため)
    detected: dict[str, dict[str, object]] = {}
    log_lines: list[str] = []

    def on_request(request) -> None:  # type: ignore[no-untyped-def]
        """検出したリクエストを記録する。

        Args:
            request: SniffedRequest。
        """
        if request.url in detected:
            return
        detected[request.url] = {
            'url': request.url,
            'kind': request.kind,
            'resource_type': request.resource_type,
            'method': request.method,
            'status': request.status,
        }
        log_lines.append(f'[detected] {request.kind} ({request.resource_type}): {request.url}')

    def on_status(message: str) -> None:
        """進捗メッセージを記録する。

        Args:
            message: ブラウザ側から届いた状態メッセージ。
        """
        log_lines.append(f'[status] {message}')

    sniffer = BrowserSniffer(
        on_request=on_request,
        on_status=on_status,
        browser_path=browser_path,
        headless='--headed' not in options,
    )
    report: dict[str, object] = {'page_url': page_url, 'seconds': seconds, 'ok': False}
    if browser_path is not None:
        # 指定されたブラウザを報告にも残す (どのブラウザで検証したかの記録)
        report['requested_browser'] = browser_path
    try:
        sniffer.start(page_url)
        time_module.sleep(seconds)
        report['browser'] = sniffer.get_browser_path()
        report['title'] = sniffer.get_page_title()
        report['ok'] = True
    except (M3U8DLError, OSError, ValueError) as exc:
        report['error'] = str(exc)
    finally:
        try:
            sniffer.stop()
        except Exception:
            # 停止時の例外は検証結果に影響しないため握りつぶす
            pass

    report['detected'] = list(detected.values())
    report['playlists'] = [item for item in detected.values() if item['kind'] == 'playlist']
    report['log'] = log_lines[-80:]
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[m3u8dl] sniff report: {out_path}')
    return 0 if report.get('ok') and report['playlists'] else 1


def _read_browser_policies() -> list[str]:
    """リモートデバッグを禁止しうるブラウザのグループポリシーを読み出す (Windows のみ)。

    Chromium 系ブラウザは「リモートデバッグを許可する」ポリシーが無効にされていると、
    リモートデバッグ付きの起動要求を拒否する。この場合ブラウザは何も出力せずに終了することがあり、
    3 種類すべてのブラウザが同じ理由で失敗する説明が付く。ここでは疑わしいポリシーの現在値を
    読み出して診断レポートに載せる (ポリシーが未設定なら「設定なし」と分かる)。

    Returns:
        ポリシーの状態を表す行のリスト。
    """
    if sys.platform != 'win32':
        return ['(Windows 以外のため未確認)']
    import winreg

    # ベンダーごとのポリシー格納先 (Chromium 系ブラウザはそれぞれ別の階層にポリシーを持つ)
    vendor_keys = [
        r'SOFTWARE\Policies\Google\Chrome',
        r'SOFTWARE\Policies\Microsoft\Edge',
        r'SOFTWARE\Policies\BraveSoftware\Brave',
        r'SOFTWARE\Manageable\Google\Chrome',
    ]
    # (値の名前, 値の意味の説明, 危険な値)
    value_targets = [
        ('RemoteDebuggingAllowed', 'リモートデバッグの許可', 0),
        ('DevToolsAvailability', 'DevTools の許可 (2=禁止)', 2),
    ]
    lines: list[str] = []
    for hive, hive_name in ((winreg.HKEY_LOCAL_MACHINE, 'HKLM'), (winreg.HKEY_CURRENT_USER, 'HKCU')):
        for key_path in vendor_keys:
            for value_name, description, blocking_value in value_targets:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    lines.append(f'  {hive_name}\\{key_path} {value_name}: 読み出し失敗 ({exc})')
                    continue
                # 禁止値が設定されていれば、この環境ではブラウザ解析を開始できない
                mark = ' ※これが失敗の原因' if value == blocking_value else ''
                lines.append(f'  {hive_name}\\{key_path} {value_name} = {value} ({description}){mark}')
    return lines if lines else ['  リモートデバッグを禁止するポリシーは設定されていない']


def _decode_browser_output(raw: bytes) -> str:
    """ブラウザの標準出力を文字化けさせずに文字列へ変換する。

    Chromium 系ブラウザは日本語環境でメッセージを UTF-8 ではなくシステムのコードページ (cp932) で
    出力することがあるため、UTF-8 で読めなければ cp932 を試す。

    Args:
        raw: ブラウザが出力したバイト列。

    Returns:
        変換後の文字列 (失敗したバイトは置換文字にする)。
    """
    for encoding in ('utf-8', 'cp932'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _run_cli_diagnose(argv: list[str]) -> int:
    """ブラウザ解析が開始できない環境を切り分けるためのレポートを 1 ファイルに書き出す。

    標準出力が使えない windowed exe でも確認できるよう、結果は指定ファイルへ書き出す。
    次の順に調べる。

    1. 実行環境 (バージョン・Python・一時ディレクトリ・管理者権限・セッション)
    2. リモートデバッグを禁止しうるブラウザのポリシー
    3. 検出したブラウザとそのバージョン
    4. ブラウザごとに「リモートデバッグ付きの起動 (ヘッドレス / 画面あり)」と
       「リモートデバッグ無しの起動」を試し、どちらで失敗しているかを切り分ける
    5. ブラウザ起動ログの末尾

    Args:
        argv: `--diagnose` 以降を含むコマンドライン引数。

    Returns:
        終了コード (0=レポートを書き出した, 2=引数エラー)。
    """
    import ctypes
    import platform
    import shutil
    import subprocess as subprocess_module
    import tempfile
    import time as time_module

    from m3u8dl import __version__
    from m3u8dl.browser import BrowserSniffer, _browser_candidates

    index = argv.index('--diagnose')
    out_text = argv[index + 1] if len(argv) > index + 1 and not argv[index + 1].startswith('--') else 'm3u8dl_diagnose.txt'
    out_path = Path(out_text).expanduser()

    lines: list[str] = []

    def add(text: str = '') -> None:
        """レポートに 1 行追加する。

        レポートファイルは UTF-8 で書くため、コンソールの文字コード (Windows の既定は cp932) で
        表せない文字があってもレポート自体は欠けない。コンソール出力だけは置換して継続する。

        Args:
            text: 追記する行。
        """
        lines.append(text)
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            # コンソールで表せない文字 (em dash など) は置換して出す
            encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
            print(text.encode(encoding, errors='replace').decode(encoding, errors='replace'), flush=True)

    creationflags = getattr(subprocess_module, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0

    # 1. 実行環境
    add(f'm3u8dl {__version__} 診断レポート')
    add(f'日時: {time_module.strftime("%Y-%m-%d %H:%M:%S")}')
    add(f'実行ファイル: {sys.executable}')
    add(f'frozen (exe): {getattr(sys, "frozen", False)}')
    add(f'Python: {platform.python_version()} / {platform.platform()}')
    add(f'一時ディレクトリ: {tempfile.gettempdir()}')
    add(f'ユーザー: {os.environ.get("USERNAME", "")} / セッション: {os.environ.get("SESSIONNAME", "")}')
    try:
        elevated = 'はい' if ctypes.windll.shell32.IsUserAnAdmin() else 'いいえ'  # type: ignore[attr-defined]
    except Exception:
        elevated = '不明'
    add(f'管理者権限: {elevated}')
    add()

    # 2. リモートデバッグを禁止しうるポリシー (設定されていればブラウザ解析は必ず失敗する)
    add('リモートデバッグ関連のポリシー:')
    for policy_line in _read_browser_policies():
        add(policy_line)
    add()

    # 3. ブラウザの検出とバージョン
    add('ブラウザ候補:')
    found: list[str] = []
    for candidate in _browser_candidates():
        if not os.path.isfile(candidate):
            add(f'  [なし] {candidate}')
            continue
        found.append(candidate)
        try:
            completed = subprocess_module.run(
                [candidate, '--version'],
                capture_output=True,
                timeout=30,
                creationflags=creationflags,
            )
            # ブラウザは日本語のメッセージを cp932 で出力することがあるため、自前でデコードする
            output = _decode_browser_output(completed.stdout or completed.stderr).strip().splitlines()
            add(f'  [あり] {candidate} - {output[0] if output else "バージョン不明"}')
        except (OSError, subprocess_module.SubprocessError) as exc:
            add(f'  [あり] {candidate} - バージョン取得失敗: {exc}')
    add()

    if not found:
        add('対応ブラウザが見つからないため、これ以上診断できません。')
        out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return 0

    # 4. ブラウザごとの起動テスト
    for browser in found:
        name = os.path.basename(browser)
        add(f'--- {name} ---')

        # (a) リモートデバッグ付き (画面あり / ヘッドレス)
        for headless in (False, True):
            mode = 'ヘッドレス' if headless else '画面あり'
            sniffer = BrowserSniffer(on_request=lambda request: None, browser_path=browser, headless=headless)
            try:
                sniffer.start('about:blank')
                add(f'  リモートデバッグ {mode}: 成功 (デバッグポート {sniffer._port})')
            except Exception as exc:
                first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
                add(f'  リモートデバッグ {mode}: 失敗 - {first_line}')
            finally:
                try:
                    sniffer.stop()
                except Exception:
                    # 診断を続けるため、停止時の例外は無視する
                    pass

        # (b) リモートデバッグ無し (ブラウザ自体がこの環境で起動できるかの切り分け)
        profile_dir = Path(tempfile.mkdtemp(prefix='m3u8dl_diag_'))
        try:
            proc = subprocess_module.Popen(
                [browser, f'--user-data-dir={profile_dir}', '--no-first-run', 'about:blank'],
                stdout=subprocess_module.DEVNULL,
                stderr=subprocess_module.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            add(f'  デバッグ無し起動: 起動できません - {exc}')
        else:
            time_module.sleep(3.0)
            return_code = proc.poll()
            if return_code is None:
                add('  デバッグ無し起動: 3 秒後も生存 (ブラウザ自体は起動できる)')
            else:
                add(f'  デバッグ無し起動: 3 秒以内に終了 (exit code={return_code}) → この環境ではブラウザ自体が起動できません')
            subprocess_module.run(
                ['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
            )
        finally:
            shutil.rmtree(profile_dir, ignore_errors=True)
        add()

    # 5. 起動ログの末尾
    log_path = Path(tempfile.gettempdir()) / 'm3u8dl_browser_launch.log'
    add(f'--- ブラウザ起動ログ ({log_path}) の末尾 ---')
    try:
        tail = log_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-30:]
        lines.extend(tail if tail else ['(空)'])
    except OSError as exc:
        add(f'(ログを読めません: {exc})')

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'[m3u8dl] 診断レポート: {out_path}', flush=True)
    return 0


def main() -> int:
    """アプリケーションを起動する。

    Returns:
        プロセスの終了コード。

    Raises:
        SystemExit: GUI 側が終了コードを返したとき。
    """
    # PyInstaller の onefile ビルドでは、子プロセス生成のためのフックが必要になる
    multiprocessing.freeze_support()

    # ソース実行時にパッケージを見つけられるようにリポジトリルートを sys.path に追加する
    repository_root = Path(__file__).resolve().parent
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

    from m3u8dl import __version__
    from m3u8dl.gui import main as gui_main
    from m3u8dl.gui import run_selftest

    # バージョン表示 (exe の --version 用途)
    if '--version' in sys.argv:
        print(f'm3u8dl {__version__}')
        return 0

    # 画面なしのダウンロード (バッチ実行と exe の実動作検証用)
    if '--download' in sys.argv:
        return _run_cli_download(sys.argv)

    # 画面なしのブラウザ解析 (exe のブラウザ解析経路の実動作検証用)
    if '--sniff' in sys.argv:
        return _run_cli_sniff(sys.argv)

    # ブラウザ解析が始められない環境の切り分けレポート (結果はファイルへ書き出す)
    if '--diagnose' in sys.argv:
        return _run_cli_diagnose(sys.argv)

    # 自己テストモード: GUI を出さずに結合検証だけ行い JSON を書き出す
    if '--selftest' in sys.argv:
        index = sys.argv.index('--selftest')
        report_path = Path(sys.argv[index + 1]) if len(sys.argv) > index + 1 else Path('selftest.json')
        return run_selftest(report_path)

    gui_main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
