# m3u8dl — m3u8 (HLS) ダウンローダー GUI

ブラウザで対象ページを開いて通信を解析し、**m3u8 (HLS) の URL を自動検出**して保存する Windows 用 GUI ツールです。
Cookie を使ったダウンロード、AES-128 で暗号化されたストリームの復号、並列ダウンロード、ffmpeg による mp4 変換に対応しています。

- 配布物: `m3u8dl.exe` (単体で動作。Python のインストールは不要)
- 依存: 標準ライブラリのみ + 同梱した pycryptodome (AES 高速化)。mp4 変換を使う場合のみ ffmpeg が必要

## 主な機能

| 機能 | 説明 |
| --- | --- |
| ブラウザ解析 (m3u8 自動検出) | 内蔵の Chrome/Edge を CDP (Chrome DevTools Protocol) で制御し、ページが読み込む `.m3u8` / `.mp4` / `.ts` / `.key` を一覧表示します。動画を再生すると実際のストリーム URL が検出されます |
| Cookie を使った DL | 手貼り付け (`a=1; b=2`)、Netscape 形式の `cookies.txt`、JSON 形式のいずれか、または**解析中のブラウザから直接取得**した Cookie を送信します |
| 並列ダウンロード | セグメントを同時接続数 (既定 8) で並列取得。リトライ (既定 3 回・指数バックオフ) つき |
| AES-128 復号 | `#EXT-X-KEY:METHOD=AES-128` のセグメントを復号 (IV 省略時はメディアシーケンスから生成)。`SAMPLE-AES` (DRM 相当) は非対応 |
| mp4 への変換 | 取得後に ffmpeg で `-c copy` 結合して mp4 化 (ffmpeg が無い場合はバイナリ結合で `.ts` として保存) |
| ライブ収録 | `#EXT-X-ENDLIST` が無いライブ配信を、停止するまでセグメント追加分だけ追記収録 |
| 進捗表示 | 進捗率・速度・残り時間・セグメント数を GUI に表示。キャンセル可能 |

## 使い方

1. `m3u8dl.exe` を起動します。
2. 上段の URL 欄に対象ページの URL (動画が再生されるページ) を入力し、**「ブラウザで解析」** を押します。
   - 解析用のブラウザが起動するので、ページ上で**動画を再生**してください (ページによってはログインが必要です)。
   - 通信の解析結果が一覧に追加されます。`.m3u8` (プレイリスト) が目的のものです。
3. 一覧の `.m3u8` をダブルクリックすると URL 欄に転記されます (直接入力しても構いません)。
4. 必要に応じて Cookie を設定します。
   - **「ブラウザからCookie取得」**: 解析中のブラウザの Cookie をそのまま使います (ログイン済みのセッションを引き継げます)。
   - 手貼り付け / **「ファイル読込」** で `cookies.txt` を読み込むこともできます。
5. 保存先パスを確認し、(必要なら) 画質・同時接続数・mp4 変換・ライブ収録を設定して **「ダウンロード開始」** を押します。

### 画質の選択

マスタープレイリストを検出した場合は、画質のコンボボックスに各バリアント (解像度/帯域) が並びます。
「自動 (最高)」「自動 (最低)」または手動で特定のバリアントを選択できます。

## ビルド方法

```bat
build_exe.bat
```

内部で以下を実行します (初回は `.venv-build` を作成)。

```bat
py -3.12 -m venv .venv-build
.venv-build\Scripts\python.exe -m pip install pyinstaller pycryptodome
.venv-build\Scripts\python.exe -m PyInstaller --noconfirm --clean m3u8dl.spec
```

生成物は `dist\m3u8dl.exe` です。

### ソースから直接実行する場合

```bat
py -3.12 main.py
```

## テスト

```bat
py -3.12 -m unittest discover -s tests -p "test_*.py" -v
```

- `test_hls.py` — プレイリスト解析 (マスター/メディア、暗号鍵、バイトレンジ、fMP4、ライブ判定)
- `test_crypto_aes.py` — AES-128-CBC 復号 (FIPS-197 既知ベクタ + 外部実装との相互検証)
- `test_cookies.py` — Cookie の各形式の解釈と変換
- `test_downloader.py` — ローカル HTTP サーバーに対する並列 DL・リトライ・AES 復号・キャンセル・部分取得
- `test_integration_local_hls.py` — ffmpeg で生成した実 HLS (平文 / AES-128) を配信して end-to-end で検証
- `test_gui_smoke.py` — GUI の起動スモークテスト
- `test_gui_download_e2e.py` — GUI (DownloaderApp) の「ダウンロード開始」経路から mp4 が完成するまでの検証 (キャンセルも含む)
- `test_browser.py` — 実ブラウザ (Chrome/Edge) を起動して CDP 解析が動くことの検証

exe の動作確認:

```bat
rem 画面を出さずに結合検証 (exe 検証用)
dist\m3u8dl.exe --selftest "%TEMP%\m3u8dl_selftest.json"

rem 画面を出さずにダウンロード (バッチ実行にも使える)
dist\m3u8dl.exe --download "https://example.com/playlist.m3u8" "C:\Users\me\Downloads\out.mp4" ^
  --cookie "a=1; b=2" --referer "https://example.com/watch" --concurrency 8
```

`--download` のオプション:

| オプション | 説明 |
| --- | --- |
| `--cookie <text\|file>` | Cookie ヘッダ (`a=1; b=2`) または Netscape 形式の `cookies.txt` |
| `--referer <url>` | Referer ヘッダ |
| `--user-agent <ua>` | User-Agent ヘッダ |
| `--concurrency <n>` | 同時接続数 (既定 8) |
| `--max-segments <n>` | 最大セグメント数 (部分保存) |
| `--ts` | mp4 に変換せず `.ts` で保存する |
| `--live` | ライブ配信を収録する (`#EXT-X-ENDLIST` まで追記) |

進捗と結果は標準出力と、出力ファイルと同じ場所の `<出力名>.log` に書かれます (コンソールの無い exe でも確認できます)。

## 構成

```
m3u8dl/
  gui.py               tkinter GUI (エントリポイント)
  browser.py           Chrome/Edge を CDP で制御して m3u8 を検出 / Cookie 取得
  websocket_client.py  RFC6455 の WebSocket クライアント (CDP 通信用・自前実装)
  downloader.py        セグメント並列取得・リトライ・復号・結合・ライブ収録
  hls.py               プレイリスト (マスター/メディア) の解析
  crypto_aes.py        AES-128-CBC 復号
  cookies.py           Cookie の解釈・変換・保存
  errors.py            例外定義
main.py                エントリポイント (--selftest / --version)
INTERFACES.md          モジュール間インターフェース契約
```

## 制約事項

- `SAMPLE-AES` (Widevine / FairPlay などの DRM が前提の方式) には対応しません。
- フェアユースの範囲 (私的利用・保存が許可されているコンテンツ) でのみ使用してください。各サービスの利用規約と著作権法を確認し、権利者の許諾が必要なコンテンツのダウンロードには使用しないでください。
- 純 Python の AES 実装にフォールバックした場合は復号が非常に遅くなります (同梱の exe は pycryptodome を同梱しているため高速です)。

## ライセンス

MIT License (LICENSE を参照)
