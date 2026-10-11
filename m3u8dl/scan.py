"""ブラウザを使わずにページから m3u8 (HLS) の URL を探すモジュール。

ブラウザを起動できない環境 (リモートデバッグがポリシーで禁止されている・管理者権限で起動した
アプリからの起動が拒否される・セキュリティ製品に止められるなど) でも最低限の検出ができるように
するためのフォールバック。ページの HTML と、そこから参照されている JavaScript を取得し、
テキスト中に書かれている `.m3u8` の URL を拾って絶対 URL に直す。

JavaScript を実行して初めて URL が決まるサイトには対応できないため補助的な手段であり、
ブラウザ解析 (BrowserSniffer) の代わりにはならない。ブラウザ解析に失敗したときの保険として使う。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# 既定の User-Agent (ブラウザから取得する場合と揃える)
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
)

# 1 ファイルあたりの読み込み上限 (巨大な JS を丸ごと読まないため)
MAX_BYTES_PER_FILE = 4 * 1024 * 1024
# 取得する JavaScript の本数の上限
MAX_SCRIPT_FILES = 12
# プレイリスト URL として扱う拡張子 (クエリ文字列が付くことがある)
_PLAYLIST_SUFFIXES = ('.m3u8', '.m3u')
# `.m3u8` を含む「URL らしい塊」を取り出す正規表現
# (前後を引用符・空白・括弧で区切り、JS の `\/` エスケープも受け付ける)
_PLAYLIST_TOKEN = re.compile(r"""[^\s"'`<>()\[\]{},;\\]+\.m3u8?[^\s"'`<>()\[\]{},;\\]*""", re.IGNORECASE)
# <script src="..."> の参照
_SCRIPT_SRC = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


@dataclass
class ScanResult:
    """ブラウザを使わない探索の結果。

    保持する情報:
        page_url: 探索したページの URL。
        playlists: 見つかったプレイリスト URL (見つかった順・重複なし)。
        fetched_urls: 実際に取得した URL (ページ本体と JavaScript)。
        errors: 取得に失敗した URL と理由 (取得できた分だけで判断できるように残す)。
    """

    page_url: str
    playlists: list[str] = field(default_factory=list)
    fetched_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _decode(data: bytes) -> str:
    """取得したバイト列を文字列に変換する。

    日本語サイトは utf-8 とは限らないため、utf-8 → cp932 → 置換付き utf-8 の順に試す。

    Args:
        data: 取得したバイト列。

    Returns:
        デコードした文字列。
    """
    for encoding in ('utf-8', 'cp932'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _fetch(url: str, headers: dict[str, str], timeout: float, limit: int) -> str:
    """URL を取得して文字列で返す。

    Args:
        url: 取得する URL。
        headers: 追加の HTTP ヘッダ。
        timeout: タイムアウト秒。
        limit: 読み込む最大バイト数。

    Returns:
        取得した内容 (文字列)。

    Raises:
        urllib.error.URLError: 取得に失敗したとき。
    """
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        # 巨大なファイルを丸ごと読まないように上限付きで読む
        return _decode(response.read(limit))


def _extract_playlists(text: str, base_url: str) -> list[str]:
    """テキスト中のプレイリスト URL を絶対 URL にして返す。

    Args:
        text: HTML または JavaScript の本文。
        base_url: 相対 URL を解決するための基準 URL。

    Returns:
        見つかったプレイリスト URL (出現順・重複なし)。
    """
    found: list[str] = []
    # JS の文字列では `/` が `\/` と書かれることがあるため、先に戻しておく
    normalised = text.replace('\\/', '/')
    for match in _PLAYLIST_TOKEN.finditer(normalised):
        token = match.group(0).strip()
        if not token.lower().endswith(_PLAYLIST_SUFFIXES) and not re.search(r'\.m3u8?', token, re.IGNORECASE):
            continue
        absolute = urllib.parse.urljoin(base_url, token)
        # プレイリスト以外の URL (html への言及など) を弾く
        path = urllib.parse.urlsplit(absolute).path.lower()
        if not path.endswith(_PLAYLIST_SUFFIXES):
            continue
        if absolute not in found:
            found.append(absolute)
    return found


def scan_page_for_playlists(
    page_url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    max_scripts: int = MAX_SCRIPT_FILES,
) -> ScanResult:
    """ページとその JavaScript を取得し、書かれているプレイリスト URL を集める。

    Args:
        page_url: 探索するページの URL。
        headers: 追加の HTTP ヘッダ (Cookie / Referer / User-Agent など)。
        timeout: 1 リクエストあたりのタイムアウト秒。
        max_scripts: 取得する JavaScript の本数の上限。

    Returns:
        探索結果 (見つかったプレイリスト URL・取得した URL・失敗の一覧)。

    Raises:
        ValueError: page_url が http / https 以外のとき。
    """
    scheme = urllib.parse.urlsplit(page_url).scheme.lower()
    if scheme not in ('http', 'https'):
        raise ValueError(f'http / https の URL を指定してください: {page_url}')

    request_headers = {'User-Agent': DEFAULT_USER_AGENT, 'Accept-Language': 'ja,en;q=0.8'}
    if headers:
        # 呼び出し側の指定 (Cookie など) を優先する
        request_headers.update({key: value for key, value in headers.items() if value})

    result = ScanResult(page_url=page_url)
    try:
        html = _fetch(page_url, request_headers, timeout, MAX_BYTES_PER_FILE)
    except (urllib.error.URLError, OSError, ValueError) as error:
        result.errors.append(f'{page_url}: {error}')
        return result
    result.fetched_urls.append(page_url)
    result.playlists.extend(_extract_playlists(html, page_url))

    # ページが参照している JavaScript も見る (プレイリスト URL は JS に書かれていることが多い)
    script_urls: list[str] = []
    for match in _SCRIPT_SRC.finditer(html):
        absolute = urllib.parse.urljoin(page_url, match.group(1))
        if absolute.startswith(('http://', 'https://')) and absolute not in script_urls:
            script_urls.append(absolute)
    for script_url in script_urls[:max_scripts]:
        try:
            script = _fetch(script_url, request_headers, timeout, MAX_BYTES_PER_FILE)
        except (urllib.error.URLError, OSError, ValueError) as error:
            result.errors.append(f'{script_url}: {error}')
            continue
        result.fetched_urls.append(script_url)
        for playlist in _extract_playlists(script, script_url):
            if playlist not in result.playlists:
                result.playlists.append(playlist)
        # hls.js などが動的に読み込む URL は、この先の JS には書かれていないことが多い。
        # プレイリストが 1 つでも見つかった時点で残りは打ち切る (無駄な取得を避ける)。
        if result.playlists:
            break

    return result
