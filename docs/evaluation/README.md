# 評価の集計（卒業論文 5.4節・5.5節 → 6.2節・6.4節）

backend ディレクトリで実行する（LLM は呼ばない）。

| ファイル | 作り方 | 内容 |
|---|---|---|
| `evaluation_<日時>.md / .json` | `scripts/evaluation_report.py`（`--db` でシステムのデータベースから） | 数学的整合率（5.4節(4)）と表10の指標 |
| `evaluation_<日時>_table10.csv` | 同上 | 表10の行ごとの値。「固有の内容（研究者判定）」列に記入する |
| `diagnosis_labels.csv` | `scripts/diagnosis_agreement.py sheet` | 誤りの種類のラベル付けの表（5.4節(3)） |
| `diagnosis_agreement_<日時>.md / .json` | `scripts/diagnosis_agreement.py score` | 研究者のラベルと Gemini の診断の一致率・κ・混同行列 |

## 誤りの種類のラベル付け（研究者の作業）

1. `diagnosis_labels.csv` を Excel などで開く。
2. 「研究者のラベル」列には，プロトタイプの書き起こし時の誤答の分類（`docs/thesis/prototype/analyze_no1.py` の ERR1，
   `analyze_trig.py` の et()）をノードに対応させた**仮の値**が入っている（「ラベルの根拠」列に対応の理由）。
   各行の初答・正解を見て，誤りの原因として最も適切なノードを確認し，違えば書き換える。空欄（要判断）の行も埋める。
   ノード名は `diagnosis_agreement_*.md` の末尾の一覧を参照。
3. 確認した行の「確認済み」列に `1` を入れて保存する（CSV のまま）。
4. `scripts/diagnosis_agreement.py score` で一致率を計算する。

`*_provisional.md` は未確認の仮のラベルで試算したもので，論文の数値には使わない。
