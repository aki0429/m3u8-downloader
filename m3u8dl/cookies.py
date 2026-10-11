"""Cookie の解析・生成・保存を行うモジュール。

HLS 配信では Cookie 認証が必要なサイトが多く、GUI から貼り付けられた文字列や
ブラウザ (CDP) から取得した Cookie、Netscape 形式の cookies.txt を統一的に扱う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# Netscape cookies.txt のデータ行フィールド数 (domain, include, path, secure, expiry, name, value)
_NETSCAPE_FIELD_COUNT = 7


@dataclass
class CookieEntry:
    """1 件の Cookie を保持する。

    Attributes:
        name: Cookie 名。
        value: Cookie 値。
        domain: 対象ドメイン。空文字なら全ドメインに送信する (ドメイン制限なし)。
        path: 対象パス。既定は '/'。
    """

    name: str
    value: str
    domain: str = ''
    path: str = '/'


def _parse_pairs(text: str) -> list[CookieEntry]:
    """'a=1; b=2' 形式の文字列を CookieEntry の一覧にする。

    セミコロン区切りだけでなく改行区切りも受け付けられるように、改行を
    セミコロンへ正規化してから分解する。

    Args:
        text: 'a=1; b=2' のような Cookie 文字列。

    Returns:
        解析結果。'=' を含まない断片は無視する。
    """
    normalized = text.replace('\r\n', '\n').replace('\n', ';')
    entries: list[CookieEntry] = []
    for chunk in normalized.split(';'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        name, _, value = chunk.partition('=')
        name = name.strip()
        value = value.strip()
        # 名前が空の断片 (=xxx) は不正なので捨てる
        if name:
            entries.append(CookieEntry(name=name, value=value))
    return entries


def _looks_like_netscape(text: str) -> bool:
    """Netscape cookies.txt 形式かどうかを判定する。

    Args:
        text: 判定対象の文字列。

    Returns:
        タブ区切りのデータ行 (7 フィールド) が 1 行以上あれば True。
    """
    for line in text.splitlines():
        stripped = line.rstrip('\r')
        if not stripped.strip():
            continue
        # '#HttpOnly_' 付きの行はデータ行として扱う。それ以外の '#' 行はコメント
        if stripped.startswith('#') and not stripped.startswith('#HttpOnly_'):
            continue
        return stripped.count('\t') >= _NETSCAPE_FIELD_COUNT - 1
    return False


def _parse_netscape(text: str) -> list[CookieEntry]:
    """Netscape cookies.txt 形式の本文を解析する。

    Args:
        text: cookies.txt の本文。

    Returns:
        解析結果。7 フィールド未満の行は読み飛ばす。
    """
    entries: list[CookieEntry] = []
    for line in text.splitlines():
        line = line.rstrip('\r')
        if not line.strip():
            continue
        # HttpOnly 行はドメインの前に印が付くので取り除く
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#'):
            continue
        parts = line.split('\t')
        # タブではなく空白区切りで保存されたファイルにも寛容に対応する
        if len(parts) < _NETSCAPE_FIELD_COUNT:
            parts = line.split()
        if len(parts) < _NETSCAPE_FIELD_COUNT:
            continue
        domain, _include_subdomains, path, _secure, _expiry, name, value = parts[:_NETSCAPE_FIELD_COUNT]
        if not name:
            continue
        entries.append(CookieEntry(name=name, value=value, domain=domain, path=path or '/'))
    return entries


def from_cdp_cookies(cookies: list[dict]) -> list[CookieEntry]:
    """CDP (Network.getAllCookies) の結果を CookieEntry の一覧に変換する。

    Args:
        cookies: CDP が返す Cookie 辞書の一覧。name / value / domain / path を読む。

    Returns:
        変換結果。name を持たないエントリは無視する。
    """
    entries: list[CookieEntry] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get('name') or '')
        if not name:
            continue
        raw_value = cookie.get('value')
        value = '' if raw_value is None else str(raw_value)
        domain = str(cookie.get('domain') or '')
        path = str(cookie.get('path') or '/')
        entries.append(CookieEntry(name=name, value=value, domain=domain, path=path))
    return entries


def parse_cookie_text(text: str) -> list[CookieEntry]:
    """貼り付けられた任意形式の Cookie テキストを解析する。

    次のいずれの形式も受理する。
      - 'a=1; b=2'
      - Netscape cookies.txt
      - Chrome 拡張などが出力する JSON 配列 / CDP の {'cookies': [...]}
      - 'Cookie: a=1; b=2'

    Args:
        text: 解析対象の文字列。

    Returns:
        解析結果。空文字や解釈できない入力では空リストを返す。
    """
    if not text:
        return []
    raw = text.strip()
    if not raw:
        return []

    # JSON 形式 (Chrome 拡張のエクスポートや CDP の結果をそのまま貼ったケース)
    if raw.startswith('[') or raw.startswith('{'):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            # CDP の応答は {'cookies': [...]} で包まれることがある
            parsed = parsed.get('cookies')
        if isinstance(parsed, list):
            cookie_dicts = [item for item in parsed if isinstance(item, dict)]
            if cookie_dicts:
                return from_cdp_cookies(cookie_dicts)

    # Netscape cookies.txt
    if _looks_like_netscape(raw):
        return _parse_netscape(raw)

    # 'Cookie: a=1; b=2' のプレフィックスを除去してから key=value を分解する
    if raw.lower().startswith('cookie:'):
        raw = raw.split(':', 1)[1].strip()
    return _parse_pairs(raw)


def _domain_matches(domain: str, host: str) -> bool:
    """Cookie のドメインが対象ホストに一致するかを判定する。

    Args:
        domain: Cookie のドメイン (先頭の '.' は無視する)。
        host: 対象 URL のホスト名。空なら不一致とする。

    Returns:
        一致すれば True。
    """
    if not domain:
        return True  # ドメイン指定なし = 全ドメインに送信
    if not host:
        return False
    normalized = domain.lstrip('.').lower()
    host = host.lower()
    if host == normalized:
        return True
    # サブドメイン一致 (.example.com は sub.example.com に一致する)
    return host.endswith('.' + normalized)


def _path_matches(cookie_path: str, request_path: str) -> bool:
    """Cookie のパスが対象 URL のパスに一致するかを判定する。

    Args:
        cookie_path: Cookie のパス。
        request_path: 対象 URL のパス。

    Returns:
        一致すれば True。
    """
    if not cookie_path or cookie_path == '/':
        return True
    return request_path.startswith(cookie_path)


def to_cookie_header(entries: list[CookieEntry], url: str = '') -> str:
    """CookieEntry の一覧を 'a=1; b=2' 形式の Cookie ヘッダ値にする。

    Args:
        entries: 変換対象の Cookie 一覧。
        url: 送信先 URL。指定するとドメインとパスでフィルタする。空なら全件を対象にする。

    Returns:
        'a=1; b=2' 形式の文字列。該当が無ければ空文字。
    """
    host = ''
    request_path = '/'
    if url:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        request_path = parsed.path or '/'

    # 同名 Cookie は後勝ちにする (辞書は挿入順を保つので順序は安定する)
    selected: dict[str, str] = {}
    for entry in entries:
        if url:
            if not _domain_matches(entry.domain, host):
                continue
            if not _path_matches(entry.path, request_path):
                continue
        selected[entry.name] = entry.value
    return '; '.join(f'{name}={value}' for name, value in selected.items())


def merge_entries(*groups: list[CookieEntry]) -> list[CookieEntry]:
    """複数の Cookie 一覧を 1 つにまとめる。

    Args:
        *groups: マージする Cookie 一覧。後ろのグループほど優先される。

    Returns:
        マージ結果。同名の Cookie は後勝ちで上書きし、出現順は最初の位置を保つ。
    """
    merged: dict[str, CookieEntry] = {}
    for group in groups:
        for entry in group:
            merged[entry.name] = entry  # 同名は後勝ち
    return list(merged.values())


def save_netscape(entries: list[CookieEntry], path: Path) -> None:
    """Cookie 一覧を Netscape cookies.txt 形式で保存する。

    Args:
        entries: 保存対象の Cookie 一覧。
        path: 保存先ファイルパス。親ディレクトリは自動作成する。
    """
    lines = [
        '# Netscape HTTP Cookie File',
        '# m3u8dl が生成した Cookie ファイル',
        '',
    ]
    for entry in entries:
        domain = entry.domain
        # 先頭が '.' のドメインはサブドメインを含む扱いにする
        include_subdomains = 'TRUE' if domain.startswith('.') else 'FALSE'
        cookie_path = entry.path or '/'
        lines.append(
            '\t'.join([domain, include_subdomains, cookie_path, 'FALSE', '0', entry.name, entry.value]),
        )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def load_file(path: Path) -> list[CookieEntry]:
    """ファイルから Cookie を読み込む。

    拡張子が .json なら JSON として、それ以外は内容から形式を自動判別して解析する。

    Args:
        path: 読み込むファイルパス。

    Returns:
        読み込んだ Cookie 一覧。
    """
    source = Path(path)
    text = source.read_text(encoding='utf-8', errors='replace')
    # 内容ベースの自動判別 (JSON / Netscape / 'a=1; b=2') に委ねる
    return parse_cookie_text(text)
