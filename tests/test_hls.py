"""m3u8dl.hls のプレイリスト解析を網羅的に検証する単体テスト。

対象は INTERFACES.md の「hls.py (担当B)」節の公開 API:
is_master_playlist / parse_master_playlist / parse_media_playlist / parse_playlist / format_duration。

マスタープレイリストの属性順序・欠落属性・相対/絶対 URL 混在、メディアプレイリストの
#EXTINF / #EXT-X-KEY / #EXT-X-BYTERANGE / #EXT-X-MAP / #EXT-X-DISCONTINUITY /
#EXT-X-MEDIA-SEQUENCE / #EXT-X-ENDLIST の各挙動を確認する。
最後に公開プレイリスト (ネットワーク依存) を skipUnless + try/except で任意検証する。
"""

from __future__ import annotations

import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

# リポジトリルートを import パスに追加する (tests/ から実行される前提)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from m3u8dl.errors import PlaylistError  # noqa: E402
from m3u8dl.hls import (  # noqa: E402
    MediaPlaylist,
    Variant,
    format_duration,
    is_master_playlist,
    parse_master_playlist,
    parse_media_playlist,
    parse_playlist,
)

# 相対 URL 解決の基準にするダミー URL (ディレクトリ階層を持たせて解決を分かりやすくする)
MASTER_BASE = 'https://example.com/hls/main/master.m3u8'
MEDIA_BASE = 'https://example.com/hls/main/media/index.m3u8'


class TestIsMasterPlaylist(unittest.TestCase):
    """マスタープレイリスト判定のテスト。"""

    def test_detects_stream_inf(self) -> None:
        """#EXT-X-STREAM-INF を含むテキストはマスタープレイリストと判定すること。"""
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow.m3u8\n'
        self.assertTrue(is_master_playlist(text))

    def test_media_playlist_is_not_master(self) -> None:
        """#EXTINF だけのメディアプレイリストはマスタープレイリストではないこと。"""
        text = '#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n'
        self.assertFalse(is_master_playlist(text))

    def test_empty_text_is_not_master(self) -> None:
        """空文字列はマスタープレイリストではないこと。"""
        self.assertFalse(is_master_playlist(''))


class TestParseMasterPlaylist(unittest.TestCase):
    """マスタープレイリスト解析のテスト。"""

    def setUp(self) -> None:
        """属性順序や属性の有無が異なる 3 バリアントを含むマスタープレイリストを用意する。"""
        self.text = (
            '#EXTM3U\r\n'
            '#EXT-X-VERSION:6\r\n'
            '\r\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2149280,AVERAGE-BANDWIDTH=2000000,'
            'RESOLUTION=1280x720,FRAME-RATE=29.97,CODECS="avc1.64001f,mp4a.40.2"\r\n'
            '720p/index.m3u8\r\n'
            '#EXT-X-STREAM-INF:RESOLUTION=640x360,BANDWIDTH=800000,CODECS="avc1.4d401e,mp4a.40.2"\r\n'
            'low/index.m3u8\r\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=6000000,NAME="FullHD",RESOLUTION=1920x1080\r\n'
            'https://cdn.example.com/fullhd.m3u8\r\n'
        )

    def test_variant_count_and_order(self) -> None:
        """記述順どおりに 3 バリアントが得られること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertEqual(len(variants), 3)
        self.assertTrue(all(isinstance(v, Variant) for v in variants))

    def test_relative_urls_are_resolved_with_urljoin(self) -> None:
        """相対 URL は基準 URL のディレクトリから urljoin で解決されること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertEqual(variants[0].url, 'https://example.com/hls/main/720p/index.m3u8')
        self.assertEqual(variants[1].url, 'https://example.com/hls/main/low/index.m3u8')

    def test_absolute_url_is_kept(self) -> None:
        """絶対 URL は urljoin 後もそのまま (別ホストのまま) であること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertEqual(variants[2].url, 'https://cdn.example.com/fullhd.m3u8')

    def test_attribute_order_does_not_matter(self) -> None:
        """属性の並び順が違っても同じ値が取り出せること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        # 1 番目: BANDWIDTH が先頭、2 番目: RESOLUTION が先頭。どちらも正しく解釈される
        self.assertEqual(variants[0].bandwidth, 2149280)
        self.assertEqual(variants[1].bandwidth, 800000)
        self.assertEqual(variants[1].resolution, '640x360')
        self.assertEqual(variants[0].average_bandwidth, 2000000)

    def test_numeric_and_quoted_attributes(self) -> None:
        """FRAME-RATE (小数) と CODECS (カンマ入りクォート値) が正しく解釈されること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertAlmostEqual(variants[0].frame_rate or 0.0, 29.97, places=2)
        self.assertEqual(variants[0].codecs, 'avc1.64001f,mp4a.40.2')

    def test_name_attribute_optional(self) -> None:
        """NAME 属性が無ければ name は None、あればその値になること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertIsNone(variants[0].name)
        self.assertIsNone(variants[1].name)
        self.assertEqual(variants[2].name, 'FullHD')

    def test_label_is_generated_for_gui(self) -> None:
        """GUI 表示用ラベルが解像度/帯域/NAME から生成されること。"""
        variants = parse_master_playlist(self.text, MASTER_BASE)
        self.assertEqual(variants[0].label, '720p 2149kbps 1280x720')
        self.assertEqual(variants[2].label, 'FullHD 1080p 6000kbps 1920x1080')

    def test_blank_line_between_tag_and_uri(self) -> None:
        """#EXT-X-STREAM-INF と URI の間に空行があっても URI を取得できること。"""
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n\nlow.m3u8\n'
        variants = parse_master_playlist(text, MASTER_BASE)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].url, 'https://example.com/hls/main/low.m3u8')

    def test_comments_around_variants_are_ignored(self) -> None:
        """バリアントの前後にコメント行があっても全バリアントが解決されること。"""
        text = (
            '#EXTM3U\n'
            '# これはコメント行\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1000\n'
            'low.m3u8\n'
            '# 中間のコメント\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000\n'
            'high.m3u8\n'
            '# 末尾のコメント\n'
        )
        variants = parse_master_playlist(text, MASTER_BASE)
        self.assertEqual([v.url for v in variants], [
            'https://example.com/hls/main/low.m3u8',
            'https://example.com/hls/main/high.m3u8',
        ])

    def test_comment_line_between_tag_and_uri(self) -> None:
        """#EXT-X-STREAM-INF と URI の間にコメント行があっても全バリアントが解決されること。

        HLS 仕様はこの位置のコメント行を明確に禁止していないが、一般的な実装
        (hls.js 等) はコメント行を読み飛ばして次の URI 行を採用する。hls.py も
        '#EXT' タグ以外のコメント行は読み飛ばすように修正済み (2026-10-12)。
        """
        text = (
            '#EXTM3U\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1000\n'
            '# コメント\n'
            'low.m3u8\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000\n'
            'high.m3u8\n'
        )
        variants = parse_master_playlist(text, MASTER_BASE)
        # コメント行を読み飛ばして両方のバリアントを取り出すこと
        self.assertEqual([v.url for v in variants], [
            'https://example.com/hls/main/low.m3u8',
            'https://example.com/hls/main/high.m3u8',
        ])

    def test_relative_dotdot_resolution(self) -> None:
        """'../' を含む相対 URL が urljoin で正しく正規化されること。"""
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n../other/playlist.m3u8\n'
        variants = parse_master_playlist(text, MASTER_BASE)
        self.assertEqual(variants[0].url, 'https://example.com/hls/other/playlist.m3u8')

    def test_stream_inf_followed_by_another_tag_is_skipped(self) -> None:
        """URI 行が来る前に別タグが挟まった STREAM-INF は(不正なので)採用しないこと。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1000\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2000\n'
            'ok.m3u8\n'
        )
        variants = parse_master_playlist(text, MASTER_BASE)
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0].bandwidth, 2000)
        self.assertEqual(variants[0].url, 'https://example.com/hls/main/ok.m3u8')

    def test_raises_when_no_stream_inf(self) -> None:
        """#EXT-X-STREAM-INF が 1 つも無ければ PlaylistError を送出すること。"""
        with self.assertRaises(PlaylistError):
            parse_master_playlist('#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n', MASTER_BASE)


class TestParseMediaPlaylist(unittest.TestCase):
    """メディアプレイリスト解析のテスト。"""

    def test_basic_segments_and_durations(self) -> None:
        """#EXTINF の秒数・タイトル・total_duration が正しく取得できること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-VERSION:3\n'
            '#EXT-X-TARGETDURATION:10\n'
            '#EXTINF:9.009,first segment\n'
            'seg0.ts\n'
            '#EXTINF:9.009,second segment\n'
            'seg1.ts\n'
            '#EXTINF:3.003,\n'
            'seg2.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertIsInstance(playlist, MediaPlaylist)
        self.assertEqual([s.url for s in playlist.segments], [
            'https://example.com/hls/main/media/seg0.ts',
            'https://example.com/hls/main/media/seg1.ts',
            'https://example.com/hls/main/media/seg2.ts',
        ])
        self.assertAlmostEqual(playlist.segments[0].duration, 9.009, places=3)
        self.assertEqual(playlist.segments[0].title, 'first segment')
        # タイトルが空 (カンマ直後に改行) のときは None になること
        self.assertIsNone(playlist.segments[2].title)
        self.assertAlmostEqual(playlist.total_duration, 21.021, places=3)

    def test_target_duration_and_version(self) -> None:
        """#EXT-X-TARGETDURATION と #EXT-X-VERSION が解析されること。"""
        text = '#EXTM3U\n#EXT-X-VERSION:4\n#EXT-X-TARGETDURATION:8\n#EXTINF:1.0,\na.ts\n#EXT-X-ENDLIST\n'
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertEqual(playlist.version, 4)
        self.assertAlmostEqual(playlist.target_duration, 8.0, places=3)

    def test_endlist_controls_is_live(self) -> None:
        """#EXT-X-ENDLIST があれば is_live=False、無ければ True になること。"""
        vod = '#EXTM3U\n#EXTINF:1.0,\na.ts\n#EXT-X-ENDLIST\n'
        live = '#EXTM3U\n#EXTINF:1.0,\na.ts\n'
        self.assertFalse(parse_media_playlist(vod, MEDIA_BASE).is_live)
        self.assertTrue(parse_media_playlist(live, MEDIA_BASE).is_live)

    def test_media_sequence_numbering(self) -> None:
        """#EXT-X-MEDIA-SEQUENCE を起点にセグメントへ連番が振られること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-MEDIA-SEQUENCE:10\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXTINF:1.0,\nb.ts\n'
            '#EXTINF:1.0,\nc.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertEqual(playlist.media_sequence, 10)
        self.assertEqual([s.media_sequence for s in playlist.segments], [10, 11, 12])

    def test_media_sequence_defaults_to_zero(self) -> None:
        """#EXT-X-MEDIA-SEQUENCE が無ければ 0 起点になること。"""
        text = '#EXTM3U\n#EXTINF:1.0,\na.ts\n#EXTINF:1.0,\nb.ts\n#EXT-X-ENDLIST\n'
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertEqual(playlist.media_sequence, 0)
        self.assertEqual([s.media_sequence for s in playlist.segments], [0, 1])

    def test_raises_when_no_segments(self) -> None:
        """セグメントが 1 つも無いメディアプレイリストは PlaylistError になること。"""
        with self.assertRaises(PlaylistError):
            parse_media_playlist('#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXT-X-ENDLIST\n', MEDIA_BASE)

    def test_blank_lines_are_ignored(self) -> None:
        """空行が混ざっていても解析できること。"""
        text = '#EXTM3U\n\n#EXTINF:1.0,\n\na.ts\n\n#EXT-X-ENDLIST\n'
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertEqual(len(playlist.segments), 1)
        self.assertEqual(playlist.segments[0].url, 'https://example.com/hls/main/media/a.ts')


class TestEncryptionKey(unittest.TestCase):
    """#EXT-X-KEY の解析テスト。"""

    def test_aes128_with_iv(self) -> None:
        """METHOD=AES-128 と IV が 16 バイトに変換され、URI が絶対 URL になること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000102030405060708090A0B0C0D0E0F\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segment = parse_media_playlist(text, MEDIA_BASE).segments[0]
        self.assertIsNotNone(segment.key)
        assert segment.key is not None
        self.assertEqual(segment.key.method, 'AES-128')
        self.assertEqual(segment.key.uri, 'https://example.com/hls/main/media/key.bin')
        self.assertEqual(segment.key.iv, bytes(range(16)))

    def test_aes128_without_iv(self) -> None:
        """IV 未指定のときは iv=None (呼び出し側が連番から生成する) になること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segment = parse_media_playlist(text, MEDIA_BASE).segments[0]
        assert segment.key is not None
        self.assertEqual(segment.key.method, 'AES-128')
        self.assertIsNone(segment.key.iv)

    def test_key_persists_until_changed(self) -> None:
        """#EXT-X-KEY は次に KEY タグが来るまで後続セグメントへ適用され続けること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key1.bin",IV=0x00\n'  # IV は不正長→None 扱い
            '#EXTINF:1.0,\na.ts\n'
            '#EXTINF:1.0,\nb.ts\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key2.bin",IV=0x000102030405060708090A0B0C0D0E0F\n'
            '#EXTINF:1.0,\nc.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        self.assertEqual(segments[0].key.uri, 'https://example.com/hls/main/media/key1.bin')  # type: ignore[union-attr]
        # 2 番目はキー未変更なので 1 番目と同じ鍵を共有する
        self.assertEqual(segments[1].key.uri, 'https://example.com/hls/main/media/key1.bin')  # type: ignore[union-attr]
        self.assertEqual(segments[2].key.uri, 'https://example.com/hls/main/media/key2.bin')  # type: ignore[union-attr]

    def test_method_none_clears_key(self) -> None:
        """METHOD=NONE 以降のセグメントは暗号化されていない (key=None) こと。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXT-X-KEY:METHOD=NONE\n'
            '#EXTINF:1.0,\nb.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        self.assertIsNotNone(segments[0].key)
        self.assertIsNone(segments[1].key)

    def test_plain_segments_have_no_key(self) -> None:
        """KEY タグが無ければ全セグメントで key=None になること。"""
        text = '#EXTM3U\n#EXTINF:1.0,\na.ts\n#EXT-X-ENDLIST\n'
        self.assertIsNone(parse_media_playlist(text, MEDIA_BASE).segments[0].key)

    def test_invalid_iv_length_becomes_none(self) -> None:
        """IV が 16 バイトでないときは None として扱うこと。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0102\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        self.assertIsNone(parse_media_playlist(text, MEDIA_BASE).segments[0].key.iv)  # type: ignore[union-attr]


class TestByteRange(unittest.TestCase):
    """#EXT-X-BYTERANGE の解析テスト。"""

    def test_explicit_offset(self) -> None:
        """'length@offset' 形式がそのまま解釈されること。"""
        text = (
            '#EXTM3U\n'
            '#EXTINF:1.0,\n'
            '#EXT-X-BYTERANGE:1500@0\n'
            'all.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segment = parse_media_playlist(text, MEDIA_BASE).segments[0]
        self.assertEqual(segment.byte_range.length, 1500)
        self.assertEqual(segment.byte_range.offset, 0)

    def test_implicit_offset_continues_from_previous(self) -> None:
        """offset 省略時は直前のサブレンジ終端から連続するオフセットになること。"""
        text = (
            '#EXTM3U\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:1000@0\nall.ts\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:500\nall.ts\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:250\nall.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        # 1 番目: 0..1000、2 番目: 1000..1500、3 番目: 1500..1750
        self.assertEqual((segments[0].byte_range.offset, segments[0].byte_range.length), (0, 1000))
        self.assertEqual((segments[1].byte_range.offset, segments[1].byte_range.length), (1000, 500))
        self.assertEqual((segments[2].byte_range.offset, segments[2].byte_range.length), (1500, 250))

    def test_implicit_offset_after_explicit_offset(self) -> None:
        """明示指定の後に offset 省略が来ても、直前の終端から継続すること。"""
        text = (
            '#EXTM3U\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:800@2000\nall.ts\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:400\nall.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        self.assertEqual((segments[1].byte_range.offset, segments[1].byte_range.length), (2800, 400))

    def test_byterange_applies_to_next_segment_only(self) -> None:
        """#EXT-X-BYTERANGE は直後の 1 セグメントだけに適用されること。"""
        text = (
            '#EXTM3U\n'
            '#EXTINF:1.0,\n#EXT-X-BYTERANGE:100@0\nall.ts\n'
            '#EXTINF:1.0,\nall.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        self.assertIsNotNone(segments[0].byte_range)
        self.assertIsNone(segments[1].byte_range)


class TestMapAndDiscontinuity(unittest.TestCase):
    """#EXT-X-MAP と #EXT-X-DISCONTINUITY の解析テスト。"""

    def test_map_sets_has_map_and_init_url(self) -> None:
        """#EXT-X-MAP の URI が init_url に絶対 URL で入り、has_map=True になること。"""
        text = (
            '#EXTM3U\n'
            '#EXT-X-MAP:URI="init.mp4"\n'
            '#EXTINF:1.0,\nseg0.m4s\n'
            '#EXT-X-ENDLIST\n'
        )
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertTrue(playlist.has_map)
        self.assertEqual(playlist.init_url, 'https://example.com/hls/main/media/init.mp4')
        # init セグメントは通常セグメント一覧には含めないこと
        self.assertEqual([s.url for s in playlist.segments],
                         ['https://example.com/hls/main/media/seg0.m4s'])

    def test_no_map_when_absent(self) -> None:
        """#EXT-X-MAP が無ければ has_map=False / init_url=None であること。"""
        text = '#EXTM3U\n#EXTINF:1.0,\nseg0.ts\n#EXT-X-ENDLIST\n'
        playlist = parse_media_playlist(text, MEDIA_BASE)
        self.assertFalse(playlist.has_map)
        self.assertIsNone(playlist.init_url)

    def test_discontinuity_marks_only_next_segment(self) -> None:
        """#EXT-X-DISCONTINUITY は直後の 1 セグメントだけに印を付けること。"""
        text = (
            '#EXTM3U\n'
            '#EXTINF:1.0,\na.ts\n'
            '#EXT-X-DISCONTINUITY\n'
            '#EXTINF:1.0,\nb.ts\n'
            '#EXTINF:1.0,\nc.ts\n'
            '#EXT-X-ENDLIST\n'
        )
        segments = parse_media_playlist(text, MEDIA_BASE).segments
        self.assertFalse(segments[0].discontinuity)
        self.assertTrue(segments[1].discontinuity)
        self.assertFalse(segments[2].discontinuity)


class TestParsePlaylistDispatch(unittest.TestCase):
    """parse_playlist の自動判別と共通バリデーションのテスト。"""

    def test_dispatches_master(self) -> None:
        """#EXT-X-STREAM-INF を含むなら ('master', バリアント一覧) を返すこと。"""
        text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow.m3u8\n'
        kind, payload = parse_playlist(text, MASTER_BASE)
        self.assertEqual(kind, 'master')
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), 1)  # type: ignore[arg-type]

    def test_dispatches_media(self) -> None:
        """#EXTINF のみなら ('media', MediaPlaylist) を返すこと。"""
        text = '#EXTM3U\n#EXTINF:1.0,\na.ts\n#EXT-X-ENDLIST\n'
        kind, payload = parse_playlist(text, MEDIA_BASE)
        self.assertEqual(kind, 'media')
        self.assertIsInstance(payload, MediaPlaylist)

    def test_raises_on_empty_text(self) -> None:
        """空文字列は PlaylistError になること。"""
        with self.assertRaises(PlaylistError):
            parse_playlist('', MASTER_BASE)

    def test_raises_on_whitespace_only(self) -> None:
        """空白のみのテキストは PlaylistError になること。"""
        with self.assertRaises(PlaylistError):
            parse_playlist('   \n  \n', MASTER_BASE)

    def test_raises_without_extm3u_header(self) -> None:
        """#EXTM3U が無いテキストは PlaylistError になること。"""
        with self.assertRaises(PlaylistError):
            parse_playlist('#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow.m3u8\n', MASTER_BASE)
        with self.assertRaises(PlaylistError):
            parse_playlist('#EXTINF:4.0,\nseg0.ts\n', MEDIA_BASE)


class TestFormatDuration(unittest.TestCase):
    """format_duration のテスト。"""

    def test_minutes_and_seconds(self) -> None:
        """1 時間未満は 'MM:SS' 形式になること。"""
        self.assertEqual(format_duration(0), '00:00')
        self.assertEqual(format_duration(59), '00:59')
        self.assertEqual(format_duration(60), '01:00')
        self.assertEqual(format_duration(754), '12:34')

    def test_hours(self) -> None:
        """1 時間以上は 'HH:MM:SS' 形式になること。"""
        self.assertEqual(format_duration(3600), '01:00:00')
        self.assertEqual(format_duration(3725), '01:02:05')
        self.assertEqual(format_duration(5025), '01:23:45')

    def test_float_is_truncated(self) -> None:
        """小数秒は切り捨てられること。"""
        self.assertEqual(format_duration(83.9), '01:23')
        self.assertEqual(format_duration(59.999), '00:59')

    def test_negative_is_clamped_to_zero(self) -> None:
        """負の値は '00:00' に丸められること。"""
        self.assertEqual(format_duration(-5), '00:00')


class TestRealPublicPlaylist(unittest.TestCase):
    """実在の公開プレイリストを使った任意検証 (ネットワーク依存のため失敗時はスキップ)。"""

    PUBLIC_URL = 'https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8'

    def _fetch(self, url: str) -> str:
        """公開プレイリストを取得する。取得できなければ skipTest でスキップする。

        Args:
            url: 取得対象の URL。

        Returns:
            プレイリスト本文。

        Raises:
            unittest.SkipTest: ネットワーク到達不能や HTTP エラーのとき。
        """
        request = urllib.request.Request(url, headers={'User-Agent': 'm3u8dl-test'})
        try:
            with urllib.request.urlopen(request, timeout=10.0) as response:  # noqa: S310
                if response.status != 200:
                    self.skipTest(f'公開プレイリストが HTTP {response.status} を返した')
                return response.read().decode('utf-8', errors='replace')
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # ネットワーク不通はスキップ
            self.skipTest(f'公開プレイリストに到達できない: {exc}')

    def test_public_master_playlist(self) -> None:
        """公開マスタープレイリストを解析して全バリアントの URL が絶対 URL になること。"""
        text = self._fetch(self.PUBLIC_URL)
        kind, payload = parse_playlist(text, self.PUBLIC_URL)
        # x36xhzz.m3u8 はマスタープレイリスト (メディアでも落ちないよう分岐を許容)
        self.assertIn(kind, ('master', 'media'))
        if kind == 'master':
            variants = payload  # type: ignore[assignment]
            self.assertGreaterEqual(len(variants), 1)  # type: ignore[arg-type]
            for variant in variants:  # type: ignore[union-attr]
                self.assertTrue(variant.url.startswith('http'))
        else:
            playlist = payload  # type: ignore[assignment]
            self.assertGreaterEqual(len(playlist.segments), 1)  # type: ignore[union-attr]
            for segment in playlist.segments:  # type: ignore[union-attr]
                self.assertTrue(segment.url.startswith('http'))


if __name__ == '__main__':
    unittest.main()
