"""m3u8dl (m3u8 ダウンローダー GUI) のエントリポイント。

起動方法:
  m3u8dl.exe                                    GUI を起動する
  m3u8dl.exe --selftest <path.json>             画面を出さずに結合検証だけを行う (exe 検証用)
  m3u8dl.exe --download <url> <out> [options]   画面を出さずにダウンロードする (バッチ / 検証用)
  m3u8dl.exe --version                          バージョンを表示する
"""

from __future__ import annotations

import json
import multiprocessing
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
        print('usage: m3u8dl.exe --sniff <page_url> [--seconds <n>] [--out <json>] [--headed]')
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
        headless='--headed' not in options,
    )
    report: dict[str, object] = {'page_url': page_url, 'seconds': seconds, 'ok': False}
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

    # 自己テストモード: GUI を出さずに結合検証だけ行い JSON を書き出す
    if '--selftest' in sys.argv:
        index = sys.argv.index('--selftest')
        report_path = Path(sys.argv[index + 1]) if len(sys.argv) > index + 1 else Path('selftest.json')
        return run_selftest(report_path)

    gui_main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
