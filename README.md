# legal-study

巨大な司法試験・予備試験教材PDFから、ChatGPT Projectがすぐにカード・ノート作成へ入れる高信頼な問題別Markdownを、**再開可能・検証可能・原文優先**で生成するためのローカルPythonアプリです。

現在の v0.1 は、最も事故が起きやすい **PDF読込レイヤ** を先に実装しています。単純に全ページOCRを掛けるのではなく、ページごとに最も信頼できる情報源を使い分けます。

## ローカルファースト

外出先のCPU-only Windows PCでも動かせるよう、コア処理はクラウドサービス、固定ドライブ文字、特定checkout先に依存しません。PDFがローカルにあれば、source snapshot・検査・レンダリング・vector mark検出・OCR対象抽出・ローカル保存まで単体で動作します。P1-BのPaddleOCR backendはCPUを明示指定し、GPU対応は将来の任意最適化です。

可変データは標準で `~/.legal-study/` 配下に置きます。別の場所を使う場合は `LEGAL_STUDY_HOME` を設定してください。詳細は [`docs/local-first.md`](docs/local-first.md) を参照してください。

入力PDFは最初に `~/.legal-study/sources/sha256/<sha256>.pdf` へcontent-addressed snapshotとして保存します。以後の解析はsnapshotだけを読み、元のOneDrive、Google Drive、USB等のファイルを再openしません。

## 最終成果物

目標フローは次のとおりです。

```text
PDF
  → immutable source snapshot
  → raw native / annotation / vector / image evidence
  → selective / surgical OCR
  → native / OCR evidence reconciliation
  → needs_review抽出
  → canonical_source.json
  → <subject>_<question>_problem.md
  → 視覚確認が必要なevidence PNGのみ添付
  → ChatGPT Projectへアップロード
```

Anki生成、Obsidian生成、Notion登録、Google Drive書込みは現在の実装scopeに含めません。

## v0.1 の方針

```text
PDF
 ├─ native text / coordinates
 ├─ PDF annotations
 ├─ flattened vector drawings (highlight / red pen)
 └─ 300dpi render
          ↓
    page quality gate
          ↓
   full-page / image-region / surgical OCR only when needed
          ↓
  reconciliation / needs_review
          ↓
  canonical_source.json
          ↓
  <subject>_<question>_problem.md
```

特に現在の教材PDFでは、PDF Annotation が0件でも、黄色・青・橙のマーカーや赤ペンが **vector drawing** として残っているページがあります。v0.1はこれを直接検出し、太い半透明ストロークとPDF本文のword boxを交差させて「マーカー候補原文」を取り出します。赤い細線は意味を推測せず、後段Vision確認用の証拠として保持します。

## なぜ全ページOCRにしないか

公開実装も調査したうえで、Markerの「PDFテキストレイヤをまず利用し、必要なページだけOCR」、DoclingのPDF-aware OCR、PaddleOCRの日本語対応を参考にしています。法律教材では、born-digital本文はOCRよりPDFテキストの方が正確なことが多い一方、貼付画像・手書き・赤字・マーカーは画像/ベクトル側にしか存在しないため、**ハイブリッド取得**が必要です。

設計詳細は [`docs/architecture.md`](docs/architecture.md) を参照してください。

## セットアップ

Windowsでは以下で初期化できます。

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\legal-study.exe init
.\.venv\Scripts\legal-study.exe doctor
```

PDFの構造だけ調べる場合、OCRモデルは不要です。

## PDFを検査する

```powershell
legal-study inspect ".\materials\論文マスター_刑法.pdf" --pages 110-116 --render-dir .\artifacts\刑法12
```

ページごとに `native / hybrid / ocr_required`、文字数、テキスト品質、画像占有率、検出vector mark数、OCR推奨、Vision確認推奨を表示します。

## ingest artifactを作る

出力先を省略すると、source SHA-256と入力設定hashを含むローカルrunディレクトリを自動作成します。同一入力はSQLite step stateとartifact hashを検証してresumeし、異なる入力を同じ出力先へ混在させません。

```powershell
legal-study ingest ".\materials\論文マスター_刑法.pdf" --subject criminal --question 12 --pages 110-116
```

出力:

- `inspection.json`
- `run_manifest.json`
- `evidence_crops.json`
- `review_manifest.json`
- `ocr.json`
- `reconciliation.json`
- `canonical_source.json`
- `<subject>_<question>_problem.md`
- `problem_validation.json`
- `renders/page-XXXX.png`
- `ocr_crops/`（壊れたUnicode mappingのsurgical cropと、独立判定した画像領域）
- `review_crops/`（赤色vector evidenceをクラスタ化したVision確認用crop）

run内のJSONが参照するrender/cropのpathはrun-relativeなPOSIX形式です。runディレクトリを移動しても、同じディレクトリ構造のままartifactを解決できます。


P1-Bまで完了済みのrunがある場合、OCRを再実行せずP1-C成果物だけを生成できます。

```powershell
legal-study finalize "C:\Users\<user>\.legal-study\runs\<existing-run>"
```

`finalize`は既存の`inspection.json`、`ocr.json`、`review_manifest.json`とSQLite run stateを検証した上で、`reconciliation.json`、`canonical_source.json`、`<subject>_<question>_problem.md`、`problem_validation.json`を生成します。PaddleOCRは呼び出さないため、既存の20分程度のOCR stepを再実行しません。

## PaddleOCRを追加する

PaddleOCR本体とCPU版PaddlePaddle runtimeをインストールし、オンライン環境で一度だけ明示的にモデルを取得します。モデルは `~/.legal-study/models/paddleocr/` に保存され、manifestのhashと実体が一致した場合だけ通常ingestで利用されます。`ingest --ocr paddle`が暗黙にdownloadを始めることはありません。

```powershell
pip install -e .[ocr]
legal-study warmup-ocr
legal-study doctor --ocr
legal-study ingest ".\materials\source.pdf" --subject criminal --question sample --pages 1-5 --ocr paddle `
  --full-page-dpi 300 --image-region-dpi 300 --surgical-dpi 450
```

v0.1ではPaddleOCR 3.7系、PaddlePaddle 3.2系、50言語対応の`PP-OCRv6` medium detection/recognition modelをCPUで使います。PaddlePaddle 3.3.xの既知oneDNN/PIR CPU回帰を避けるため3.3未満へ固定し、oneDNNも無効化します。engine/library/runtime version、model名・hash、device、実行日時、DPI、crop bbox/padding、前処理、画像hash、pixel→PDF座標transformをartifactとrun inputへ保存します。DPIは300/450/600を選択でき、初期値はfull page/image region=300、surgical=450です。

## 重要な安全設計

- PDF SHA-256を保存し、PDF差替え時の古い抽出結果再利用を防止。
- 元PDFをcontent-addressed storeへsnapshot後、全解析をsnapshotだけから実行。
- run input hash、step status、input/output hash、retry、error、versionをSQLiteに保存。
- JSON・render・cropをatomicに公開し、hash不一致の旧artifactは削除せず`orphans/`へ退避。
- full-page OCRは「native textが不足/低品質」のページに限定。
- ページに十分なnative textがあっても、独立したsubstantive image regionはOCR対象にできる。
- 壊れたUnicode mappingはページ全体ではなく該当spanを**surgical OCR**対象にする。
- OCR結果は常に別Evidenceとして保存し、native textを置換しない。
- マーカー色から法的役割を自動推測しない。
- 赤ペンを「修正」と決め打ちしない。
- Vision確認が必要なページを明示的に残す。
- copyrighted PDFそのものや生成renderはGit管理しない。
- コアPDF処理にクラウド接続を必須としない。

## P1-C

P1-Cではnative/OCR evidenceを保守的に照合し、`AUTO_VERIFIED / NEEDS_REVIEW / UNRESOLVED`を分離します。比較用のUnicode/空白正規化は原文とは別フィールドで保持し、OCRがnative textを書き換えることはありません。

生成された`problem.md`はChatGPT Projectへ渡す主要成果物です。視覚判断が必要な赤vector、OCR-only画像、境界未確定marker等は`Needs Review`へ残し、run-relativeなevidence参照を保持します。

`canonical_source.json`は個々のmarker fragmentを`markers`へ保持したまま、安全に連続性を確認できた同色・同一行のfragmentだけを`logical_markers`へ統合します。`problem.md`はlogical markerを表示し、構成元marker IDとraw vector IDをprovenanceとして残します。OCR Supplementsにはfull-page/image-region OCRだけを載せ、surgical OCRはreconciliation evidenceとして保持します。

最終調整はローカルCodexで行いやすいよう、各処理を独立モジュールに分割してあります。
