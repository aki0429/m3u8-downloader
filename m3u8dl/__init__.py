"""m3u8 / HLS ダウンローダー GUI (m3u8dl)。

ブラウザで対象ページを解析して m3u8 を検出し、Cookie を付けて保存する GUI ツール。
"""

from m3u8dl.errors import (
    BrowserError,
    DownloadCancelled,
    DownloadError,
    M3U8DLError,
    PlaylistError,
)

__version__ = '1.0.1'

__all__ = [
    'BrowserError',
    'DownloadCancelled',
    'DownloadError',
    'M3U8DLError',
    'PlaylistError',
    '__version__',
]
