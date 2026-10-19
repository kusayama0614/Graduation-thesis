# 誤りの種類の診断の一致率（5.4節(3)）

- ラベルの表: docs/evaluation/diagnosis_labels.csv
- 作成: 2026-10-19 04:01
- 使った行: 86 / 88（除外: ラベルが空欄 2）
- **未確認の仮のラベルも含めた試算**

| モデル | 対象 | 件数 | 一致 | 一致率 | κ |
|---|---|---|---|---|---|
| gemini-3.1-pro-preview | Gemini が候補から選択 | 51 | 44 | 0.863 | 0.832 |
| | 　No.1 | 41 | 38 | 0.927 | 0.903 |
| | 　No.8 | 10 | 6 | 0.600 | 0.000 |
| gemini-3.1-pro-preview | 候補が1つ（参考） | 0 | 0 | — | — |
| gemini-3.8-flash | Gemini が候補から選択 | 51 | 39 | 0.765 | 0.714 |
| | 　No.1 | 41 | 36 | 0.878 | 0.836 |
| | 　No.8 | 10 | 3 | 0.300 | 0.000 |
| gemini-3.8-flash | 候補が1つ（参考） | 35 | 35 | 1.000 | 1.000 |
| | 　No.8 | 35 | 35 | 1.000 | 1.000 |

モデル間の一致（gemini-3.1-pro-preview と gemini-3.8-flash，候補から選択した行）: 46/53（0.868，κ=0.837）

## gemini-3.1-pro-preview：混同行列（行: 研究者のラベル，列: 診断）

| | application_modeling | discriminant_setup | discriminant_sign | double_root | exact_value | inequality_manipulation | trig_table_reading |
|---|---|---|---|---|---|---|---|
| application_modeling | 6 |  |  |  |  |  | 4 |
| discriminant_setup |  | 5 | 1 | 1 |  |  |  |
| discriminant_sign |  | 1 | 4 |  |  |  |  |
| double_root |  |  |  | 15 |  |  |  |
| exact_value |  |  |  |  | 5 |  |  |
| inequality_manipulation |  |  |  |  |  | 9 |  |
| trig_table_reading |  |  |  |  |  |  |  |

### 一致しなかった行

| 演習 | 生徒 | 小問 | 初答 | 研究者 | 診断 | 診断の理由 |
|---|---|---|---|---|---|---|
| No.1 | Q022 | 練習29 | 2, 0, 0 | discriminant_setup | discriminant_sign | 3つ目の問題において、判別式がD=0となる場合の実数解または共有点の個数を1個ではなく0個と誤認しているため。 |
| No.1 | Q097 | 練習29 | 2, 2, 1 | discriminant_sign | discriminant_setup | 正解が0個である問題に対して2個と答えていることから、判別式の立式や計算を誤って判別式を正の値にしてしまったと考えられるため。 |
| No.1 | Q133 | 練習31 | m=-1 のとき x=2，m=3 のとき x=6 | discriminant_setup | double_root | 定数mの値を求める手順とそのときの重解xの値を求める手順を混同し、mとxの値を入れ替えて解答しているため。 |
| No.8 | T01 | P5 | 171, 460.85 | application_modeling | trig_table_reading | 計算後の有効数字や四捨五入などの指定された桁の処理を行わずに、そのまま小数で解答しているため。 |
| No.8 | T32 | P6 | 26.6 | application_modeling | trig_table_reading | 正解と非常に近い値が算出されているため、立式ではなく三角比の表から値を読み取る際に誤った値を参照してしまったと考えられるから。 |
| No.8 | T38 | P6 | 26.6 | application_modeling | trig_table_reading | 生徒の解答は正解とわずかに異なる数値であり、三角比の表から値を読み取る際に見間違えたと考えられるため。 |
| No.8 | T46 | P5 | 170, 460 | application_modeling | trig_table_reading | 生徒の解答は正解に非常に近い値であることから立式自体は正しく行えており、三角比の表から読み取った値の計算における丸め方や桁の扱いを誤ったと考えられるため。 |

## gemini-3.8-flash：混同行列（行: 研究者のラベル，列: 診断）

| | application_modeling | discriminant_setup | discriminant_sign | double_root | exact_value | inequality_manipulation | trig_table_reading |
|---|---|---|---|---|---|---|---|
| application_modeling | 3 |  |  |  |  |  | 7 |
| discriminant_setup |  | 3 | 1 | 3 |  |  |  |
| discriminant_sign |  |  | 5 |  |  |  |  |
| double_root |  |  |  | 15 |  |  |  |
| exact_value |  | 1 |  |  | 4 |  |  |
| inequality_manipulation |  |  |  |  |  | 9 |  |
| trig_table_reading |  |  |  |  |  |  |  |

### 一致しなかった行

| 演習 | 生徒 | 小問 | 初答 | 研究者 | 診断 | 診断の理由 |
|---|---|---|---|---|---|---|
| No.1 | Q022 | 練習29 | 2, 0, 0 | discriminant_setup | discriminant_sign | 3つ目の問題において、判別式が0のときに実数解（共有点）の個数が1個となる対応を取り違え、0個と誤解している可能性が高いため。 |
| No.1 | Q057 | 練習31 | m=6，m=-2 | discriminant_setup | double_root | 問題で求められている重解の計算を行っておらず、定数mの値のみを答えているため。 |
| No.1 | Q105 | 練習30 | (1<=m)&(m<=5) | exact_value | discriminant_setup | 判別式に代入する係数の読み取りや立式を誤り、本来1次不等式になるはずのところをmについての2次不等式などとして解いてしまっているため。 |
| No.1 | Q113 | 練習31 | m=-2，m=6 | discriminant_setup | double_root | 重解となる定数mの値の符号が誤っているだけでなく、問われている重解xの値を計算して答えることができていません。 |
| No.1 | Q133 | 練習31 | m=-1 のとき x=2，m=3 のとき x=6 | discriminant_setup | double_root | 正解におけるパラメータ m と重解 x の値を逆に答えており，重解の条件や重解 x=−b/2a の扱いに誤りがあると考えられるため。 |
| No.8 | T01 | P5 | 171, 460.85 | application_modeling | trig_table_reading | 1つ目の値は正解しているものの、2つ目の値で指定された四捨五入などの桁の処理を行わずに小数のまま答えているため。 |
| No.8 | T03 | P5 | 141, 470 | application_modeling | trig_table_reading | 一方の値は正しく求められているものの、もう一方の値で三角比の表の読み取りやその後の数値処理を誤っているため。 |
| No.8 | T32 | P6 | 26.6 | application_modeling | trig_table_reading | 立式自体は概ね合っているものの、三角比の表の値を読み取る際に行や数値を誤ったか、端数処理の段階で数値を誤って読み取って計算したと考えられます。 |
| No.8 | T38 | P6 | 26.6 | application_modeling | trig_table_reading | 正解の26.8に対して26.6とわずかな数値のずれが生じており、三角比の表から読み取った値や小数の端数処理を誤ったと考えられます。 |
| No.8 | T42 | P6 | 26.2 | application_modeling | trig_table_reading | 正解と値がわずかに異なっていることから、三角比の表から値を読み取る際に行や列、数値を誤って参照したと考えられます。 |
| No.8 | T46 | P5 | 170, 460 | application_modeling | trig_table_reading | 正解に対して数値がわずかにずれており、三角比の表の読み取りや端数処理などの桁の扱いに誤りがあると考えられるため。 |
| No.8 | T50 | P5 | 171, 500 | application_modeling | trig_table_reading | 1つ目の値は正しく求められていることから立式はできており、2つ目の値において三角比の表の読み取りや指定された桁への四捨五入などの処理を誤ったと考えられます。 |

## ノード名

- `discriminant_setup`: 判別式に代入する係数の読み取りと立式
- `discriminant_sign`: 判別式の符号と実数解の個数・共有点の個数の対応
- `double_root`: 重解の条件と重解 x=−b/2a の計算
- `intersection`: 放物線と直線の共有点（連立して解く）
- `inequality_solution`: 2次不等式の解とグラフの対応（判別式が負・0の場合を含む）
- `inequality_manipulation`: 不等号の向き（負の数で割る）と等号の有無
- `exact_value`: 分数・無理数を近似せずに扱う
- `system_inequality`: 連立不等式の共通範囲（数直線）
- `word_problem_modeling`: 文章題の立式と変数の範囲
- `root_location`: 解の配置（判別式・軸・端点の値の条件）
- `absolute_value_cases`: 絶対値を含む関数の場合分けとグラフ
- `side_identification`: 対辺・隣辺・斜辺の特定（図の向きによらない）
- `special_angle_values`: 30°・45°・60°の三角比の値
- `trig_table_reading`: 三角比の表の読み取りと桁の扱い
- `inverse_table_lookup`: 三角比の値から角度を求める
- `application_modeling`: 仰角・傾斜などの場面の図示と立式
- `mutual_relations`: sin²θ+cos²θ=1 などの相互関係
- `complementary_angle`: 90°−θ の三角比
