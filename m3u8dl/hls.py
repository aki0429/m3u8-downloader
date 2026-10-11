"""M3U8 (HLS) プレイリストの解析モジュール。

マスタープレイリスト (#EXT-X-STREAM-INF) とメディアプレイリスト (#EXTINF) の両方を扱い、
セグメントの相対 URL 解決・暗号鍵情報・バイトレンジ・fMP4 初期化セグメントを読み取る。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from m3u8dl.errors import PlaylistError

# 属性リスト (KEY=VALUE,KEY="VALUE") を分解するための正規表現
# 値はダブルクォート囲みとそれ以外の両方を許容する
_ATTRIBUTE_PATTERN = re.compile(r'([A-Za-z0-9\-]+)=("[^"]*"|[^,]*)')


@dataclass
class ByteRange:
    """#EXT-X-BYTERANGE で指定された部分取得の範囲を保持する。

    Attributes:
        length: 取得するバイト数。
        offset: 取得開始位置 (バイト)。省略時は前回のサブレンジの直後を意味する。
    """

    length: int
    offset: int


@dataclass
class EncryptionKey:
    """#EXT-X-KEY の情報を保持する。

    Attributes:
        method: 暗号化方式 ('AES-128' / 'SAMPLE-AES' / 'NONE')。
        uri: 鍵の絶対 URL。METHOD=NONE のときは None。
        iv: 16 バイトの初期化ベクトル。未指定のときは None (セグメント連番から生成する)。
        key_format: KEYFORMAT 属性。'identity' が既定。
    """

    method: str
    uri: str | None = None
    iv: bytes | None = None
    key_format: str | None = None


@dataclass
class Segment:
    """メディアプレイリスト中の 1 セグメントを保持する。

    Attributes:
        url: セグメントの絶対 URL。
        duration: #EXTINF の秒数。
        title: #EXTINF のタイトル部 (カンマ以降)。
        byte_range: #EXT-X-BYTERANGE の範囲。指定が無ければ None。
        key: このセグメントに適用される暗号鍵。暗号化されていなければ None。
        discontinuity: 直前に #EXT-X-DISCONTINUITY があったか。
        media_sequence: このセグメントのメディアシーケンス番号。
    """

    url: str
    duration: float
    title: str | None = None
    byte_range: ByteRange | None = None
    key: EncryptionKey | None = None
    discontinuity: bool = False
    media_sequence: int = 0


@dataclass
class Variant:
    """マスタープレイリスト中の 1 バリアントストリームを保持する。

    Attributes:
        url: バリアントプレイリストの絶対 URL。
        bandwidth: BANDWIDTH 属性 (bps)。
        average_bandwidth: AVERAGE-BANDWIDTH 属性 (bps)。
        resolution: RESOLUTION 属性 ('1280x720')。
        codecs: CODECS 属性。
        name: NAME 属性。無ければ解像度などから生成する。
        frame_rate: FRAME-RATE 属性。
        label: GUI の一覧に表示するラベル。
    """

    url: str
    bandwidth: int | None = None
    average_bandwidth: int | None = None
    resolution: str | None = None
    codecs: str | None = None
    name: str | None = None
    frame_rate: float | None = None
    label: str = ''


@dataclass
class MediaPlaylist:
    """メディアプレイリスト全体を保持する。

    Attributes:
        url: プレイリスト自身の絶対 URL。
        segments: セグメントの並び (再生順)。
        media_sequence: #EXT-X-MEDIA-SEQUENCE の値。
        target_duration: #EXT-X-TARGETDURATION の値 (秒)。
        is_live: #EXT-X-ENDLIST が無ければ True (ライブ配信中)。
        version: #EXT-X-VERSION の値。
        has_map: #EXT-X-MAP (fMP4 の初期化セグメント) を含むか。
        init_url: #EXT-X-MAP の URI (絶対 URL)。無ければ None。
        total_duration: 全セグメントの合計秒数。
    """

    url: str
    segments: list[Segment] = field(default_factory=list)
    media_sequence: int = 0
    target_duration: float = 0.0
    is_live: bool = True
    version: int | None = None
    has_map: bool = False
    init_url: str | None = None
    total_duration: float = 0.0


def _parse_attributes(value: str) -> dict[str, str]:
    """属性リスト文字列を辞書に分解する。

    Args:
        value: '#EXT-X-STREAM-INF:' や '#EXT-X-KEY:' の後ろ側の文字列。

    Returns:
        属性名をキー、値 (クォートは除去) を値とする辞書。
    """
    attributes: dict[str, str] = {}
    for key, raw in _ATTRIBUTE_PATTERN.findall(value):
        text = raw.strip()
        # ダブルクォートで囲まれた値はクォートを外して格納する
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            text = text[1:-1]
        attributes[key.upper()] = text
    return attributes


def _to_int(value: str | None) -> int | None:
    """文字列を int に変換する。変換できないときは None を返す。

    Args:
        value: 変換対象の文字列。

    Returns:
        変換結果。失敗時は None。
    """
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: str | None) -> float | None:
    """文字列を float に変換する。変換できないときは None を返す。

    Args:
        value: 変換対象の文字列。

    Returns:
        変換結果。失敗時は None。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_iv(value: str | None) -> bytes | None:
    """#EXT-X-KEY の IV 属性を 16 バイトに変換する。

    Args:
        value: '0x...' 形式の 16 進文字列。

    Returns:
        16 バイトの IV。指定が無い/不正なときは None。
    """
    if not value:
        return None
    text = value[2:] if value.lower().startswith('0x') else value
    # 16 進として読めない場合は IV 未指定として扱う (セグメント連番から生成される)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    if len(raw) != 16:
        return None
    return raw


def _build_variant_label(variant: Variant) -> str:
    """GUI 表示用のラベル文字列を組み立てる。

    Args:
        variant: ラベルを生成するバリアント。

    Returns:
        '1080p 6000kbps 1920x1080' のような表示用文字列。
    """
    parts: list[str] = []
    if variant.name:
        parts.append(variant.name)
    if variant.resolution:
        height = variant.resolution.split('x')[-1]
        if height.isdigit():
            parts.append(f'{height}p')
    if variant.bandwidth:
        parts.append(f'{variant.bandwidth // 1000}kbps')
    if variant.resolution:
        parts.append(variant.resolution)
    if not parts:
        return variant.url
    return ' '.join(parts)


def is_master_playlist(text: str) -> bool:
    """マスタープレイリストかどうかを判定する。

    Args:
        text: プレイリストの本文。

    Returns:
        #EXT-X-STREAM-INF を含むなら True (マスタープレイリスト)。
    """
    return '#EXT-X-STREAM-INF' in text


def parse_master_playlist(text: str, base_url: str) -> list[Variant]:
    """マスタープレイリストを解析してバリアント一覧を返す。

    Args:
        text: マスタープレイリストの本文。
        base_url: 相対 URL を解決するための基準 URL。

    Returns:
        バリアントの一覧 (記述順)。

    Raises:
        PlaylistError: #EXT-X-STREAM-INF が 1 つも見つからなかったとき。
    """
    variants: list[Variant] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith('#EXT-X-STREAM-INF:'):
            continue
        attributes = _parse_attributes(stripped[len('#EXT-X-STREAM-INF:'):])
        # STREAM-INF の直後の「空行でもコメント行でもない行」が URI になる
        uri = ''
        for candidate in lines[index + 1:]:
            candidate = candidate.strip()
            if not candidate:
                continue
            if candidate.startswith('#'):
                # コメント行は読み飛ばす (URI 行の手前にコメントが挟まる実装があるため)。
                # '#EXT' で始まるタグが来た場合は STREAM-INF に対応する URI が無いと判断して打ち切る
                if candidate.startswith('#EXT'):
                    break
                continue
            uri = candidate
            break
        if not uri:
            continue
        variants.append(
            Variant(
                url=urljoin(base_url, uri),
                bandwidth=_to_int(attributes.get('BANDWIDTH')),
                average_bandwidth=_to_int(attributes.get('AVERAGE-BANDWIDTH')),
                resolution=attributes.get('RESOLUTION'),
                codecs=attributes.get('CODECS'),
                name=attributes.get('NAME'),
                frame_rate=_to_float(attributes.get('FRAME-RATE')),
            ),
        )
    if not variants:
        raise PlaylistError('マスタープレイリストに #EXT-X-STREAM-INF が見つかりません')
    for variant in variants:
        variant.label = _build_variant_label(variant)
    return variants


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    """メディアプレイリストを解析してセグメント一覧を返す。

    Args:
        text: メディアプレイリストの本文。
        base_url: 相対 URL を解決するための基準 URL。

    Returns:
        解析結果。ENDLIST が無ければ is_live=True。

    Raises:
        PlaylistError: セグメントが 1 つも見つからなかったとき。
    """
    playlist = MediaPlaylist(url=base_url)
    segments: list[Segment] = []

    # 現在有効な暗号鍵 (#EXT-X-KEY は次の KEY タグまで継続して適用される)
    current_key: EncryptionKey | None = None
    # #EXT-X-BYTERANGE で offset を省略したときに使う、直前のサブレンジ末尾位置
    last_byte_range_end = 0

    # #EXTINF で確定するが、まだ URI 行が来ていないセグメントの一時保持領域
    pending_duration = 0.0
    pending_title: str | None = None
    pending_byte_range: ByteRange | None = None
    pending_discontinuity = False

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('#'):
            if stripped.startswith('#EXT-X-MEDIA-SEQUENCE:'):
                playlist.media_sequence = _to_int(stripped.split(':', 1)[1]) or 0
            elif stripped.startswith('#EXT-X-TARGETDURATION:'):
                playlist.target_duration = _to_float(stripped.split(':', 1)[1]) or 0.0
            elif stripped.startswith('#EXT-X-VERSION:'):
                playlist.version = _to_int(stripped.split(':', 1)[1])
            elif stripped.startswith('#EXT-X-MAP:'):
                attributes = _parse_attributes(stripped[len('#EXT-X-MAP:'):])
                # URI 属性のみを初期化セグメントとして扱う (BYTERANGE 付きは稀なので無視する)
                if attributes.get('URI'):
                    playlist.init_url = urljoin(base_url, attributes['URI'])
                    playlist.has_map = True
            elif stripped.startswith('#EXT-X-KEY:'):
                attributes = _parse_attributes(stripped[len('#EXT-X-KEY:'):])
                method = (attributes.get('METHOD') or 'NONE').upper()
                if method == 'NONE':
                    # METHOD=NONE 以降は平文に戻る
                    current_key = None
                else:
                    uri = attributes.get('URI')
                    current_key = EncryptionKey(
                        method=method,
                        uri=urljoin(base_url, uri) if uri else None,
                        iv=_parse_iv(attributes.get('IV')),
                        key_format=attributes.get('KEYFORMAT'),
                    )
            elif stripped.startswith('#EXT-X-BYTERANGE:'):
                raw = stripped.split(':', 1)[1].strip()
                if '@' in raw:
                    length_text, offset_text = raw.split('@', 1)
                    length = _to_int(length_text) or 0
                    offset = _to_int(offset_text) or 0
                else:
                    # offset 省略時は直前のサブレンジの直後から取得する
                    length = _to_int(raw) or 0
                    offset = last_byte_range_end
                pending_byte_range = ByteRange(length=length, offset=offset)
                last_byte_range_end = pending_byte_range.offset + pending_byte_range.length
            elif stripped.startswith('#EXT-X-DISCONTINUITY'):
                # 次のセグメントのみに印を付ける
                pending_discontinuity = True
            elif stripped.startswith('#EXTINF:'):
                raw = stripped.split(':', 1)[1]
                duration_text, _, title_text = raw.partition(',')
                pending_duration = _to_float(duration_text) or 0.0
                pending_title = title_text.strip() or None
            elif stripped.startswith('#EXT-X-ENDLIST'):
                playlist.is_live = False
            # それ以外のタグ (#EXT-X-PROGRAM-DATE-TIME 等) はここでは使わない
            continue

        # コメントでもタグでもない行 = セグメント URI
        segments.append(
            Segment(
                url=urljoin(base_url, stripped),
                duration=pending_duration,
                title=pending_title,
                byte_range=pending_byte_range,
                key=current_key,
                discontinuity=pending_discontinuity,
                media_sequence=playlist.media_sequence + len(segments),
            ),
        )
        # 1 セグメント分の一時状態をリセットする (暗号鍵は継続するので消さない)
        pending_duration = 0.0
        pending_title = None
        pending_byte_range = None
        pending_discontinuity = False

    if not segments:
        raise PlaylistError('メディアプレイリストにセグメントが見つかりません')

    playlist.segments = segments
    playlist.total_duration = sum(segment.duration for segment in segments)
    return playlist


def parse_playlist(text: str, base_url: str) -> tuple[str, list[Variant] | MediaPlaylist]:
    """プレイリストの種別を自動判別して解析する。

    Args:
        text: プレイリストの本文。
        base_url: 相対 URL を解決するための基準 URL。

    Returns:
        ('master', バリアント一覧) または ('media', メディアプレイリスト)。

    Raises:
        PlaylistError: どちらとしても解析できなかったとき。
    """
    if not text.strip():
        raise PlaylistError('プレイリストが空です')
    if '#EXTM3U' not in text:
        raise PlaylistError('#EXTM3U ヘッダがありません (m3u8 プレイリストではない可能性があります)')
    if is_master_playlist(text):
        return 'master', parse_master_playlist(text, base_url)
    return 'media', parse_media_playlist(text, base_url)


def format_duration(seconds: float) -> str:
    """秒数を 'HH:MM:SS' または 'MM:SS' 形式に整形する。

    Args:
        seconds: 整形する秒数。

    Returns:
        整形後の文字列。
    """
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'
