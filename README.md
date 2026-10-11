# m3u8dl — m3u8 (HLS) ダウンローダー GUI

ブラウザで対象ページを開いて通信を解析し、**m3u8 (HLS) の URL を自動検出**して保存する Windows 用 GUI ツールです。
Cookie を使ったダウンロード、AES-128 で暗号化されたストリームの復号、並列ダウンロード、ffmpeg による mp4 変換に対応しています。

- 配布物: `m3u8dl.exe` (単体で動作。Python のインストールは不要)
- 依存: 標準ライブラリのみ + 同梱した pycryptodome (AES 高速化)。mp4 変換を使う場合のみ ffmpeg が必要

## 主な機能

| 機能 | 説明 |
| --- | --- |
| ブラウザ解析 (m3u8 自動検出) | 内蔵の Chrome/Edge/Brave を CDP (Chrome DevTools Protocol) で制御し、ページが読み込む `.m3u8` / `.mp4` / `.ts` / `.key` を一覧表示します。ページを開くだけでなく**再生操作 (再生ボタンのクリックや `play()`) も自動で行う**ため、クリックして再生するタイプのプレーヤーでも実際のストリーム URL が検出されます。解析は既定で**画面を出さずにバックグラウンドで動作**します。起動できなかったブラウザは自動で次の候補 (Chrome → Edge → Brave) に切り替えます |
| Cookie を使った DL | 手貼り付け (`a=1; b=2`)、Netscape 形式の `cookies.txt`、JSON 形式のいずれか、または**解析中のブラウザから直接取得**した Cookie を送信します |
| 並列ダウンロード | セグメントを同時接続数 (既定 8) で並列取得。リトライ (既定 3 回・指数バックオフ) つき |
| AES-128 復号 | `#EXT-X-KEY:METHOD=AES-128` のセグメントを復号 (IV 省略時はメディアシーケンスから生成)。`SAMPLE-AES` (DRM 相当) は非対応 |
| mp4 への変換 | 取得後に ffmpeg で `-c copy` 結合して mp4 化 (ffmpeg が無い場合はバイナリ結合で `.ts` として保存) |
| ライブ収録 | `#EXT-X-ENDLIST` が無いライブ配信を、停止するまでセグメント追加分だけ追記収録 |
| 進捗表示 | 進捗率・速度・残り時間・セグメント数を GUI に表示。キャンセル可能 |

## 使い方

1. `m3u8dl.exe` を起動します。
2. 上段の URL 欄に対象ページの URL (動画が再生されるページ) を入力し、**「ブラウザで解析」** を押します。
   - 解析は既定で**バックグラウンド (画面を出さずに) 実行**され、ページの再生操作も自動で行います。操作は不要です。
   - ログインや年齢確認などページ側の操作が必要なサイトでは、**「解析中にブラウザの画面を表示」** にチェックを入れてから解析してください。画面が出た状態でページを操作すると、その通信も解析対象になります。
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
- `test_integration_fmp4.py` — ffmpeg で生成した fMP4 (init.mp4 + .m4s) の HLS を配信し、mp4 出力・`--ts` 指定時の扱い・ライブ収録での初期化セグメントの扱いを検証
- `test_scan.py` — ブラウザを使わない URL 探索 (HTML / JavaScript / 相対 URL / エラー時の扱い) と GUI のフォールバック経路
- `test_play_drive.py` — 再生の自動操作 (メインフレームと入れ子 iframe への操作、失敗時の扱い、繰り返しと停止) の検証
- `test_gui_analysis_mode.py` — GUI が解析時にどの起動モード (バックグラウンド / 画面表示) を選ぶかの検証
- `test_gui_smoke.py` — GUI の起動スモークテスト
- `test_gui_download_e2e.py` — GUI (DownloaderApp) の「ダウンロード開始」経路から mp4 が完成するまでの検証 (キャンセルも含む)
- `test_gui_analysis_e2e.py` — GUI の「ブラウザで解析」経路でプレイリストとバリアントが一覧に反映され、停止できることの検証
- `test_browser.py` — 実ブラウザ (Chrome/Edge/Brave) を起動して CDP 解析が動くことの検証 (起動失敗時の診断・フォールバック・`--browser` の解決も含む)
- `test_console_encoding.py` — コンソールへ出力するソースが cp932 で表せる文字だけで書かれていることの確認

exe の動作確認:

```bat
rem 画面を出さずに結合検証 (exe 検証用)
dist\m3u8dl.exe --selftest "%TEMP%\m3u8dl_selftest.json"

rem 画面を出さずにブラウザ解析だけを実行 (検出した URL を JSON に書き出す)
dist\m3u8dl.exe --sniff "https://hls-js.netlify.app/demo/" --seconds 20 --out sniff.json

rem 使用するブラウザを指定して解析する (Chrome 以外での動作確認にも使える)
dist\m3u8dl.exe --sniff "https://hls-js.netlify.app/demo/" --seconds 20 --out sniff_edge.json --browser edge

rem ブラウザを使わずに、ページとその JavaScript から m3u8 の URL を探す
dist\m3u8dl.exe --scan "https://example.com/watch/123" --out scan.json

rem 「ブラウザで解析」が始まらない環境の切り分けレポートを書き出す
dist\m3u8dl.exe --diagnose "%USERPROFILE%\Desktop\m3u8dl_diagnose.txt"

rem 画面を出さずにダウンロード (バッチ実行にも使える)
dist\m3u8dl.exe --download "https://example.com/playlist.m3u8" "C:\Users\me\Downloads\out.mp4" ^
  --cookie "a=1; b=2" --referer "https://example.com/watch" --concurrency 8
```

`--sniff` のオプション:

| オプション | 説明 |
| --- | --- |
| `--seconds <n>` | 解析する秒数 (既定 25) |
| `--out <json>` | 結果の JSON の出力先 (既定 `sniff.json`) |
| `--headed` | ヘッドレスではなく実際にウィンドウを表示して解析する |
| `--no-drive` | ページの再生の自動操作 (再生ボタンのクリック / `play()`) を行わない。切り分け用 |
| `--browser <name\|path>` | 使用するブラウザを指定する (`chrome` / `edge` / `brave`、または実行ファイルのパス)。省略時は Chrome → Edge → Brave の順に自動で試す |

`--scan` のオプション:

| オプション | 説明 |
| --- | --- |
| `--out <json>` | 結果の JSON の出力先 (既定 `scan.json`) |
| `--cookie <text\|file>` / `--referer <url>` / `--user-agent <ua>` | 取得に使うヘッダ |

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
  browser.py           Chrome / Edge / Brave を CDP で制御して m3u8 を検出 / Cookie 取得
  scan.py              ブラウザを使わずにページと JavaScript から m3u8 の URL を探す (フォールバック)
  websocket_client.py  RFC6455 の WebSocket クライアント (CDP 通信用・自前実装)
  downloader.py        セグメント並列取得・リトライ・復号・結合・ライブ収録
  hls.py               プレイリスト (マスター/メディア) の解析
  crypto_aes.py        AES-128-CBC 復号
  cookies.py           Cookie の解釈・変換・保存
  errors.py            例外定義
main.py                エントリポイント (--selftest / --sniff / --scan / --diagnose / --download / --version)
INTERFACES.md          モジュール間インターフェース契約
```

## トラブルシューティング

### 「ブラウザが起動直後に終了しました」と出る

解析用ブラウザ (Chrome / Edge / Brave) が起動直後に終了しています。ツールは自動で次の候補ブラウザへ切り替えるため、この表示が出るときは**すべての候補ブラウザで失敗**しています。原因の切り分けは以下で行えます。

- `--diagnose` で切り分けレポートを書き出せます。ブラウザごとに「リモートデバッグ付きの起動 (画面あり / ヘッドレス)」「リモートデバッグ無しの起動」を実際に試し、リモートデバッグを禁止するポリシーの有無も確認します。

  ```bat
  dist\m3u8dl.exe --diagnose "%USERPROFILE%\Desktop\m3u8dl_diagnose.txt"
  ```

- エラーメッセージに終了コード、ブラウザ自身の出力の末尾、一時プロファイルの状態がそのまま表示されます (ファイル数 0 ならブラウザは初期化前に終了しています)。
- ブラウザの起動ログは `%TEMP%\m3u8dl_browser_launch.log` に追記されます (起動引数とブラウザの出力)。
- Chrome / Edge が**自動更新の適用待ち**の状態だと起動直後に終了することがあります。ツールは起動プロセスが終了しても数秒間デバッグポートの応答を待ち、それでもだめならサンドボックスを無効化した起動も試します。ブラウザを手動で起動して更新を終わらせてから再試行すると確実です。
- リモートデバッグがポリシーで禁止されている環境では、どのブラウザでも解析はできません。`--diagnose` のポリシー欄を確認し、ブラウザ側の設定 (またはシステム管理者) でリモートデバッグを許可するか、Cookie を貼り付けてのダウンロード機能を使ってください。
- 画面ありの起動だけが失敗する環境のために、起動に失敗すると**ヘッドレスでも自動的に再試行**します (ヘッドレスでも検出は同じように動きます)。
- それでもブラウザが起動できない場合は、**ページの内容から URL を直接探すフォールバック**が動きます。GUI では「ブラウザで解析」の失敗時に自動で実行され、ボタン「URL を抽出」で単独実行もできます。コマンドラインでは `--scan` です。ページの HTML と参照している JavaScript に書かれた `.m3u8` を拾うだけなので、JavaScript が URL を組み立てるサイトでは見つかりません。

  ```bat
  dist\m3u8dl.exe --scan "https://example.com/watch/123" --out scan.json
  ```

### バリアント (画質) の URL が 404 になる

マスタープレイリストに書かれているバリアントの URL 自体が間違っている配信があります (例: 実在しない `mastermaster_video10000k.m3u8` を指している)。この場合はツール側では取得できないため、`--download` のエラーメッセージに出る解決後の URL を確認し、正しい URL を直接指定してください。

## 制約事項

- `SAMPLE-AES` (Widevine / FairPlay などの DRM が前提の方式) には対応しません。
- fMP4 (`#EXT-X-MAP` あり) の配信は、各セグメントが単体では解析できない断片のため、セグメントをまとめて mp4 として保存します (`--ts` を指定しても `.mp4` になります)。
- フェアユースの範囲 (私的利用・保存が許可されているコンテンツ) でのみ使用してください。各サービスの利用規約と著作権法を確認し、権利者の許諾が必要なコンテンツのダウンロードには使用しないでください。
- 純 Python の AES 実装にフォールバックした場合は復号が非常に遅くなります (同梱の exe は pycryptodome を同梱しているため高速です)。

## ライセンス

MIT License (LICENSE を参照)
