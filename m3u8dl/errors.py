"""m3u8 ダウンローダー GUI の共通例外定義。"""


class M3U8DLError(Exception):
    """m3u8dl 全体の基底例外。"""


class PlaylistError(M3U8DLError):
    """プレイリストの解析に失敗したときに送出する例外。"""


class DownloadError(M3U8DLError):
    """セグメントの取得やファイル出力に失敗したときに送出する例外。"""


class BrowserError(M3U8DLError):
    """ブラウザの起動や CDP 接続に失敗したときに送出する例外。"""


class DownloadCancelled(M3U8DLError):
    """ユーザー操作によってダウンロードが中断されたときに送出する例外。"""
