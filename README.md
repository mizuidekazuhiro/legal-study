# legal-study

司法試験・予備試験の論文学習を、**再開可能・検証可能・原文優先**で処理するためのローカルPythonアプリです。

現在の v0.1 は、最も事故が起きやすい **PDF読込レイヤ** を先に実装しています。単純に全ページOCRを掛けるのではなく、ページごとに最も信頼できる情報源を使い分けます。

## ローカルファースト

外出先のPCでも動かせるよう、コア処理は Google Drive / Notion / Obsidian / 固定ドライブ文字に依存しません。PDFがローカルにあれば、検査・レンダリング・vector mark検出・OCR対象抽出・ローカル保存まで単体で動作する設計です。

可変データは標準で `~/.legal-study/` 配下に置きます。別の場所を使う場合は `LEGAL_STUDY_HOME` を設定してください。詳細は [`docs/local-first.md`](docs/local-first.md) を参照してください。

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
   OCR only when needed
          ↓
  Vision review manifest
          ↓
 later: canonical source → Anki / Obsidian / Notion
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

出力先を省略すると、PDFのSHA-256を含むローカルrunディレクトリを自動作成します。

```powershell
legal-study ingest ".\materials\論文マスター_刑法.pdf" --subject criminal --question 12 --pages 110-116
```

出力:

- `inspection.json`
- `review_manifest.json`
- `ocr.json`
- `renders/page-XXXX.png`
- `ocr_crops/`（壊れたUnicode mappingが疑われる箇所だけを450dpiで切り出し）
- `review_crops/`（赤ペンstrokeをクラスタ化したVision確認用crop）

## PaddleOCRを追加する

PaddleOCR本体に加えて、使用環境に合うPaddle inference runtimeが必要です。外出先でオフライン利用する場合は、出発前にモデルを一度取得してローカルキャッシュを準備します。

```powershell
pip install -e .[ocr]
legal-study ingest ".\materials\source.pdf" --subject criminal --question sample --pages 1-5 --ocr paddle
```

v0.1ではPaddleOCRは `PP-OCRv6` / `lang="japan"` を明示して使います。モデルの自動更新で認識挙動が変わらないよう、後続版でモデル名・runtime versionもrun manifestへ固定します。

## 重要な安全設計

- PDF SHA-256を保存し、PDF差替え時の古い抽出結果再利用を防止。
- OCRは「native textが不足/低品質」のページに限定。
- 壊れたUnicode mappingはページ全体ではなく該当spanを**surgical OCR**対象にする。
- マーカー色から法的役割を自動推測しない。
- 赤ペンを「修正」と決め打ちしない。
- Vision確認が必要なページを明示的に残す。
- copyrighted PDFそのものや生成renderはGit管理しない。
- コアPDF処理にクラウド接続を必須としない。

## 次の実装

1. native text / PaddleOCR / Vision の差分照合
2. マーカー開始文字・終了文字の厳密確定
3. `canonical_source.json` の生成
4. SQLiteによるrun state / resume
5. 刑法Anki/Obsidian generator
6. Google Drive Inbox / Notion connector

最終調整はローカルCodexで行いやすいよう、各処理を独立モジュールに分割してあります。
