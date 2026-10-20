"""フェーズB: 積み上げ型の解説生成（卒業論文 3.4節・3.5節，表5）

Chain 1  問題文を誰にとっても省略できない解答ステップに分解し，各ステップが依存する前提知識ノードを
         付ける（LLM。出力は JSON Schema でノード名の選択肢に制約）。1問につき1回だけ実行する。
Chain 2  ステップごとに，生徒の定着度から「含める要素」をプログラムの条件分岐で決め（LLM は呼ばない），
         その方針で LLM が説明を一から書く。
Chain 3  各ステップの説明を順に結合する（プログラム）。
Chain 4  説明中の「左辺 = 右辺」の計算を LLM が式として抜き出し，sympy で検算する。誤りがあれば
         どの式が誤りで正しい値は何かを添えて Chain 2 に差し戻す。上限を超えたら教員の確認に回す。

教員が作ったマスター解説の TeX（テンプレート）がある問題では，テンプレートの「手順」を解答ステップとし，
Chain 2 は各手順の本文を土台に内容だけを個別化する。答・考え方・図・注意などの体裁はプログラムがテンプレートの
まま組み立てる（Chain 3）。

詳しさの決定（3.4節の方式(1)）: ステップが依存するノードのうち最も定着度の低いものの値 c に応じて，
含める要素を切り替える。閾値は ELEMENT_RULES に集約しており，判断の理由は全て trace に残る。
"""
from __future__ import annotations

import difflib
import inspect
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from app.learning import graph, polish, tex_template


# 手順の文章を書く・整える呼び出しの思考の深さ。計算は sympy で確かめるので深く考えさせる必要はなく，
# 既定（深い思考）のままだと1回の応答に数分かかり，Gemini の時間切れ（504 DEADLINE_EXCEEDED）でジョブが止まって見える
WRITING_THINKING_LEVEL = os.getenv('GEMINI_WRITING_THINKING_LEVEL', 'low').strip() or None


def generate_writing(llm, system: str, user: str) -> str:
    """文章を書く呼び出し（思考を浅くする。思考の深さを受け付けないクライアントにはそのまま渡す）。"""
    if WRITING_THINKING_LEVEL and 'thinking_level' in inspect.signature(llm.generate_text).parameters:
        return llm.generate_text(system=system, user=user, temperature=0.3, thinking_level=WRITING_THINKING_LEVEL)
    return llm.generate_text(system=system, user=user, temperature=0.3)
from app.learning.llm import LLMClient
from app.learning.profile import confidence_of
from app.utils.math_verification import (
    _IDENTIFIER, check_claim, check_relation_combination, check_relation_step, is_math_expression, judge_answer, solved_piece_variable, solved_values,
    symbol_name,
)

# ---------------------------------------------------------------- 詳しさの決定規則（Chain 2）

DETAIL_THRESHOLD = 0.5            # 草稿 3.4節の閾値: これを下回るノードがあれば「詳しく書く」
DEFINITION_REVIEW_THRESHOLD = 0.35
SIMILAR_CHECK_THRESHOLD = 0.2
CAUTION_CONFIDENCE_LIMIT = 0.7    # 誤りの記録があっても，定着度がこれ以上なら注意は添えない
MAX_ATTEMPTS = 3                  # 1ステップあたりの生成回数の上限（初回 + 差し戻し2回）

ELEMENT_RULES = [
    # (要素, 説明)
    ('result', '常に含める'),
    ('reasoning', f'最も弱いノードの定着度 < {DETAIL_THRESHOLD}'),
    ('definition_review', f'最も弱いノードの定着度 < {DEFINITION_REVIEW_THRESHOLD}'),
    ('similar_check', f'最も弱いノードの定着度 < {SIMILAR_CHECK_THRESHOLD}'),
    ('error_caution', f'誤りの記録があるノードの定着度 < {CAUTION_CONFIDENCE_LIMIT}'),
]

ELEMENT_LABELS = {
    'result': '結果と使う公式',
    'reasoning': '途中の式変形を省略しない説明',
    'definition_review': '前提知識の定義・公式の意味の復習',
    'similar_check': '類題での確認',
    'error_caution': 'その生徒の誤りの傾向への注意',
}

MASTER_ELEMENTS = ['result', 'reasoning', 'definition_review']


class ExplanationError(ValueError):
    pass


def decide_step(step: Dict[str, Any], node_states: Dict[str, Any],
                error_tendencies: Dict[str, int]) -> Dict[str, Any]:
    """ステップに含める要素を，定着度の数値に対する条件分岐で決める（LLM は呼ばない）。"""
    labels = graph.node_labels()
    nodes = step.get('nodes') or []
    if not nodes:
        return {'elements': ['result'], 'weakest_node': None, 'confidence': None, 'evidence_count': None,
                'node_confidences': {}, 'cautions': [],
                'reasons': ['依存する前提知識ノードがないため，結果と使う公式だけを簡潔に書く']}

    confidences = {n: confidence_of(node_states, n) for n in nodes}
    weakest = min(nodes, key=lambda n: confidences[n][0])
    c, evidence = confidences[weakest]
    weakest_desc = f'{weakest}（{labels.get(weakest, weakest)}）の定着度 {c:.2f}（根拠 {evidence} 問）'

    elements = ['result']
    reasons: List[str] = []
    if c < DETAIL_THRESHOLD:
        elements.append('reasoning')
        reasons.append(f'{weakest_desc} が閾値 {DETAIL_THRESHOLD} を下回るため，途中の式変形を省略せずに書く')
    else:
        reasons.append(f'依存するノードの定着度がすべて {DETAIL_THRESHOLD} 以上（最小は {weakest_desc}）のため，'
                       '結果と使う公式だけを簡潔に書く')
    if c < DEFINITION_REVIEW_THRESHOLD:
        elements.append('definition_review')
        reasons.append(f'定着度 {c:.2f} < {DEFINITION_REVIEW_THRESHOLD} のため，前提知識の定義・公式の意味を復習する')
    if c < SIMILAR_CHECK_THRESHOLD:
        elements.append('similar_check')
        reasons.append(f'定着度 {c:.2f} < {SIMILAR_CHECK_THRESHOLD} のため，類題での確認を添える')

    cautions = []
    for node in nodes:
        count = int(error_tendencies.get(node, 0))
        node_c = confidences[node][0]
        if count >= 1 and node_c < CAUTION_CONFIDENCE_LIMIT:
            cautions.append({'node': node, 'label': labels.get(node, node), 'error_count': count})
            reasons.append(f'{node}（{labels.get(node, node)}）で誤りの記録が {count} 回あり，'
                           f'定着度 {node_c:.2f} < {CAUTION_CONFIDENCE_LIMIT} のため，誤りへの注意を添える')
    if cautions:
        elements.append('error_caution')

    return {
        'elements': elements,
        'weakest_node': weakest,
        'confidence': c,
        'evidence_count': evidence,
        'node_confidences': {n: {'confidence': v[0], 'evidence_count': v[1]} for n, v in confidences.items()},
        'cautions': cautions,
        'reasons': reasons,
    }


def master_decision(step: Dict[str, Any]) -> Dict[str, Any]:
    return {'elements': list(MASTER_ELEMENTS), 'weakest_node': None, 'confidence': None, 'evidence_count': None,
            'node_confidences': {}, 'cautions': [],
            'reasons': ['マスター解説: 生徒によらず行間を全て埋めて書く']}


def plan_explanation(steps: List[Dict[str, Any]], node_states: Dict[str, Any],
                     error_tendencies: Dict[str, int]) -> List[Dict[str, Any]]:
    return [{'step_id': s['id'], 'title': s['title'], 'nodes': s.get('nodes', []),
             **decide_step(s, node_states, error_tendencies)} for s in steps]


# ---------------------------------------------------------------- Chain 1: 解答ステップへの分解

CHAIN1_SYSTEM = (
    'あなたは高等学校数学の解答を，誰にとっても省略できない解答ステップに分解します。'
    '分解は生徒によらない共通のものです。各ステップには，そのステップが依存する前提知識を，'
    '与えられた選択肢の中からだけ選んで付けます（依存するものがなければ空にします）。'
    'ステップは解答の順に並べ，各ステップの結果（得られる式や値）を目安として書きます。'
)


def _chain1_schema(unit: str) -> Dict[str, Any]:
    return {
        'type': 'object',
        'properties': {
            'steps': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string'},
                        'description': {'type': 'string'},
                        'result': {'type': 'string'},
                        'nodes': {'type': 'array', 'items': {'type': 'string', 'enum': graph.node_ids(unit)}},
                    },
                    'required': ['title', 'description', 'result', 'nodes'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['steps'],
        'additionalProperties': False,
    }


def validate_steps(steps: Any, unit: str) -> List[Dict[str, Any]]:
    """Chain 1 の出力や教員が編集したステップを検証し，id を 1 から振り直す。"""
    if not isinstance(steps, list) or not steps:
        raise ExplanationError('解答ステップが1つ以上必要です')
    known = set(graph.node_ids(unit))
    normalized = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ExplanationError(f'ステップ{index}: オブジェクトである必要があります')
        title = str(step.get('title', '')).strip()
        if not title:
            raise ExplanationError(f'ステップ{index}: title が空です')
        nodes = step.get('nodes') or []
        if not isinstance(nodes, list):
            raise ExplanationError(f'ステップ{index}: nodes はノード名の配列である必要があります')
        nodes = [str(n).strip() for n in nodes]
        unknown = [n for n in nodes if n not in known]
        if unknown:
            raise ExplanationError(f'ステップ{index}: 単元 {unit} に無いノード {", ".join(unknown)} が含まれています')
        normalized.append({
            'id': index,
            'title': title[:200],
            'description': str(step.get('description', '')).strip()[:2000],
            'result': str(step.get('result', '')).strip()[:500],
            'nodes': list(dict.fromkeys(nodes)),
        })
    return normalized


def decompose_problem(llm: LLMClient, unit: str, problem_text: str) -> List[Dict[str, Any]]:
    labels = graph.node_labels()
    options = '\n'.join(f'- {n}: {labels[n]}' for n in graph.node_ids(unit))
    user = (f'単元: {graph.get_unit(unit)["label"]}\n\n問題文:\n{problem_text}\n\n'
            f'前提知識の選択肢（ノード名: 内容）:\n{options}')
    out = llm.generate_json(system=CHAIN1_SYSTEM, user=user, schema=_chain1_schema(unit),
                            schema_name='solution_steps', temperature=0.0)
    return validate_steps(out.get('steps'), unit)


TAG_SYSTEM = (
    'あなたは高等学校数学の解答の各手順が依存する前提知識を判定します。手順は教員が書いた解答解説の'
    'とおりに与えるので，手順を変えずに，各手順が依存する前提知識を与えられた選択肢の中からだけ選びます'
    '（依存するものがなければ空にします）。'
)


def _tag_schema(unit: str) -> Dict[str, Any]:
    return {
        'type': 'object',
        'properties': {'steps': {'type': 'array', 'items': {
            'type': 'object',
            'properties': {'step': {'type': 'integer'},
                           'nodes': {'type': 'array', 'items': {'type': 'string', 'enum': graph.node_ids(unit)}}},
            'required': ['step', 'nodes'], 'additionalProperties': False}}},
        'required': ['steps'], 'additionalProperties': False,
    }


def tag_template_steps(llm: LLMClient, unit: str, problem_text: str, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """テンプレートの手順（Chain 1 の分解の代わり）に，依存する前提知識ノードだけを付ける。"""
    labels = graph.node_labels()
    options = '\n'.join(f'- {n}: {labels[n]}' for n in graph.node_ids(unit))
    outline = '\n\n'.join(f'手順{s["id"]}: {s["title"]}\n{s.get("description", "")}' for s in steps)
    user = (f'単元: {graph.get_unit(unit)["label"]}\n\n問題:\n{problem_text}\n\n解答の手順（TeX）:\n{outline}\n\n'
            f'前提知識の選択肢（ノード名: 内容）:\n{options}\n\n手順の番号 step ごとに nodes を答えてください。')
    out = llm.generate_json(system=TAG_SYSTEM, user=user, schema=_tag_schema(unit),
                            schema_name='template_step_nodes', temperature=0.0)
    by_step = {int(item.get('step', 0)): item.get('nodes') or [] for item in out.get('steps', []) if isinstance(item, dict)}
    tagged = [{**step, 'nodes': by_step.get(step['id'], [])} for step in steps]
    return validate_steps(tagged, unit)


# ---------------------------------------------------------------- Chain 2: 決めた方針で説明を書く

CHAIN2_SYSTEM = (
    'あなたは高等学校数学の解答解説を，解答ステップごとに書きます。指示された「含める要素」だけに'
    '従って書き，方針を変えたり指示にない要素を足したりしないでください。'
    '計算は必ず「左辺 = 右辺」の形で書きます。生徒に語りかける「です・ます」調で，見出しは付けません。'
)


def _element_instructions(decision: Dict[str, Any], step: Dict[str, Any]) -> List[str]:
    labels = graph.node_labels()
    elements = decision['elements']
    lines = ['このステップで得られる結果と，使う公式・考え方を示す。']
    if 'reasoning' in elements:
        lines.append('途中の式変形を省略せず，一行ごとに何をしているかを添えて書く。')
    else:
        lines.append('結果と使う公式だけを簡潔に書く（2〜3文程度）。途中の式変形の詳しい説明は書かない。')
    if 'definition_review' in elements:
        node_text = '，'.join(labels.get(n, n) for n in step.get('nodes', []))
        lines.append(f'最初に，このステップが依存する前提知識（{node_text}）の定義や公式の意味を短く復習する。')
    for caution in decision.get('cautions', []):
        lines.append(f'この生徒は「{caution["label"]}」で誤りの記録が{caution["error_count"]}回ある。'
                     'その誤りが起きやすい箇所に，混同しないための注意を一言添える。')
    if 'similar_check' in elements:
        lines.append('最後に，理解を確かめる簡単な類題を1問，答えを付けて示す。')
    lines.append(BLIND_RULE)
    return lines


def write_step(llm: LLMClient, problem: Dict[str, Any], steps: List[Dict[str, Any]], step: Dict[str, Any],
               decision: Dict[str, Any], previous_texts: List[str], feedback: Optional[str] = None) -> str:
    labels = graph.node_labels()
    outline = '\n'.join(f'{s["id"]}. {s["title"]}' for s in steps)
    previous = '\n\n'.join(f'(ステップ{i}) {t}' for i, t in enumerate(previous_texts, start=1)) or '（なし）'
    instructions = '\n'.join(f'- {line}' for line in _element_instructions(decision, step))
    node_text = '，'.join(labels.get(n, n) for n in step.get('nodes', [])) or '（なし）'
    user = (
        f'問題文:\n{problem["problem_text"]}\n\n解答ステップの全体:\n{outline}\n\n'
        f'これまでのステップの説明:\n{previous}\n\n'
        f'今回書くステップ: {step["id"]}. {step["title"]}\n内容: {step.get("description", "")}\n'
        f'結果の目安: {step.get("result", "")}\n依存する前提知識: {node_text}\n\n'
        f'含める要素（この指示だけに従う）:\n{instructions}'
    )
    if feedback:
        user += f'\n\n前回書いた説明には次の誤りがありました。誤りを直して，このステップを書き直してください。\n{feedback}'
    return generate_writing(llm, CHAIN2_SYSTEM, user).strip()


TEMPLATE_SYSTEM = (
    'あなたは高等学校数学の解答解説を，教員が書いたマスター解説の「手順」を土台にして生徒ごとに書き直します。'
    '体裁（TeX の書き方，文体，式の書き方）はマスター解説の手順と同じにし，指示された「含める要素」に従って'
    '内容だけを変えます。出力は手順の本文の TeX だけで，\\tejun などの見出しや答・考え方は書きません。'
    '数式は $...$，\\[...\\]，align* 環境で書き，計算は「左辺 = 右辺」の形にします。'
    '文はマスター解説と同じ常体（である調）で書き，書き足す部分も「〜です」「〜します」「〜しましょう」などの敬体は使いません。'
    '数学の用語は正しい漢字で書きます（「下に凸」「上に凸」など。「とつ」と書かない）。'
)


# 生徒には個別化した解説とマスター解説の区別がつかないようにする（比較のため）
BLIND_RULE = ('この生徒の記録（誤りの回数や定着度）や，この解説が生徒に合わせて書かれていることには触れない。'
              '注意や復習は「あなたは〜」「記録では〜」とせず，誰にでも当てはまる一般的な書き方にする。')


def _template_instructions(decision: Dict[str, Any], step: Dict[str, Any]) -> List[str]:
    labels = graph.node_labels()
    elements = decision['elements']
    lines = ['マスター解説のこの手順の結果（得られる式や値）と，使う公式・考え方は必ず残す。']
    if 'reasoning' in elements:
        lines.append('この生徒はこの手順が依存する前提知識の定着が弱い。マスター解説で省いている途中の式変形を補い，'
                     '1行ごとに何をしているかを短く添えて，マスター解説より詳しく書く。')
    else:
        lines.append('この生徒はこの手順が依存する前提知識が定着している。マスター解説の本文を簡潔にし，'
                     '結果と使う公式を中心に書く（マスター解説より長くしない）。')
    if 'definition_review' in elements:
        node_text = '，'.join(labels.get(n, n) for n in step.get('nodes', []))
        lines.append(f'本文の最初に \\fukushu{{...}} で，前提知識（{node_text}）の定義や公式の意味を1〜2文で復習する。')
    for caution in decision.get('cautions', []):
        lines.append(f'この生徒は「{caution["label"]}」で誤りの記録が{caution["error_count"]}回ある。本文の最後に '
                     '\\begin{chui} ... \\end{chui} で，その誤りが起きやすい箇所に向けた注意を添える。注意は要点を1つに'
                     '絞って2文以内にし，下の「マスター解説の注意」や，この解説で既に書いた注意と同じ内容なら書かない'
                     '（「両辺に $-1$ をかけると不等号の向きが逆になる」に対する「両辺に負の数をかけると…」のような'
                     '言い換えも同じ内容とみなし，元からある注意に任せる）。')
    if 'similar_check' in elements:
        lines.append('本文の最後に \\ruidai{...} で，理解を確かめる簡単な類題を1問，答えを付けて示す。')
    lines.append('⟦図1⟧ のような印は図・表の位置を表す。印は消さずに同じ位置に残す。')
    lines.append(BLIND_RULE)
    return lines


def write_template_step(llm: LLMClient, problem: Dict[str, Any], steps: List[Dict[str, Any]], step: Dict[str, Any],
                        decision: Dict[str, Any], body: str, previous_texts: List[str],
                        feedback: Optional[str] = None) -> str:
    outline = '\n'.join(f'手順{s["id"]}: {s["title"]}' for s in steps)
    previous = '\n\n'.join(f'(手順{i}) {t}' for i, t in enumerate(previous_texts, start=1)) or '（なし）'
    instructions = '\n'.join(f'- {line}' for line in _template_instructions(decision, step))
    master_cautions = '\n'.join(f'- {c}' for c in polish.template_cautions(problem.get('template'))) or '（なし）'
    user = (
        f'問題文:\n{problem["problem_text"]}\n\n手順の全体:\n{outline}\n\n'
        f'マスター解説の注意（同じ趣旨の注意は書かない）:\n{master_cautions}\n\n'
        f'この生徒向けにここまでに書いた手順:\n{previous}\n\n'
        f'今回書くステップ: {step["id"]}. {step["title"]}\n'
        f'マスター解説のこの手順の本文（TeX。これを土台にする）:\n{body}\n\n'
        f'含める要素（この指示だけに従う）:\n{instructions}'
    )
    if feedback:
        user += f'\n\n前回書いた本文には次の誤りがありました。誤りを直して，この手順の本文を書き直してください。\n{feedback}'
    text = generate_writing(llm, TEMPLATE_SYSTEM, user).strip()
    # 「```latex」などで囲まれて返ってきたときは外し，決まった誤字（下にとつ → 下に凸 など）を直す
    return polish.fix_text(re.sub(r'^```[a-zA-Z]*\n|\n?```$', '', text).strip())


# ---------------------------------------------------------------- Chain 4: 計算の抜き出しと検算

EXTRACTION_SYSTEM = (
    'あなたは解説文から計算の主張を抜き出し，数式処理システム（sympy）で扱える式に書き換えます。'
    '書かれている値を直したり計算をやり直したりしてはいけません。誤っていても書かれたとおりに抜き出します。'
)

EXTRACTION_RULES = (
    '- 解説文中の「左辺 = 右辺」の形の計算を，書かれた順に全て抜き出す。\n'
    '- 連続した等式 a = b = c は，a = b と b = c の2つに分ける。\n'
    '- lhs・rhs は sympy で解釈できる表記にする（掛け算 *，累乗 ^，平方根 sqrt()，角度は sin(30°) のように°を付ける，'
    '変数は半角英字。θ は t に置き換える）。lhs_text・rhs_text には解説中の元の表記を入れる。\n'
    '- 文字を定義する式（D = b^2 - 4ac など）もそのまま含める。不等式は claims に含めない。\n'
    '- 場合分けで同じ文字を場合ごとに別の式で表したもの（x ≦ 0, 2 ≦ x のとき y = x^2-2x，0 < x < 2 のとき y = -(x^2-2x)）は，'
    'どちらも claims に入れてよい（それぞれの場合の式として扱う）。\n'
    '- 文字は，解説中で文字として書かれている記号（D，m，x など）だけを使う。「塔全体の高さ」のように言葉で書かれた量に'
    '自分で文字を割り当ててはいけない。その場合は数値の計算だけを抜き出す'
    '（例:「塔全体の高さ = 14.004 + 1.5 = 15.504」→ lhs「14.004 + 1.5」，rhs「15.504」）。\n'
    '- relations には，不等式と，文字について解く方程式（「(m+6)(m-2)=0」「m=2, -6」など）を書かれた順に全て入れる。'
    'expr は sympy で解釈できる表記，text は元の表記とする。その式が直前の relations の式を変形して得られたもの'
    '（移項・両辺を割る・因数分解して解くなど）なら transformed_from_previous を true，新しい条件や別の条件を'
    '書き始めたもの（D > 0 という条件を立てる，別の条件を並べる など）なら false にする。'
    'このステップの最初の式が，前のステップの最後の式を変形したものなら true にする。\n'
    '- 2次不等式を解くときの途中の方程式（「x^2-4x+1=0 の解は x=2±√3」）の後に書いた不等式の答えは，'
    '元の不等式を変形したものとして transformed_from_previous を true にする。\n'
    '- 複数の解をカンマなどで並べたもの（「x = 1-√6, 1+√6」「x ≦ 1-√6，1+√6 ≦ x」）は，別々の relations に分けず，'
    '「x = 1-sqrt(6), 1+sqrt(6)」のように1つの relation の expr にまとめる。\n'
    "- b' のようなプライム付きの文字は，そのまま b' と書く。\n"
    '- LaTeX 記法（\\frac，\\sqrt，$ など）は使わず，49/8 や sqrt(3) のように書く。\n'
    '- final_answer には，解説が問題の最終的な答えとして述べている内容を，単位を付けずに書く（無ければ空文字）。'
)

_EXTRACTION_SCHEMA = {
    'type': 'object',
    'properties': {
        'claims': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'lhs': {'type': 'string'}, 'rhs': {'type': 'string'},
                    'lhs_text': {'type': 'string'}, 'rhs_text': {'type': 'string'},
                },
                'required': ['lhs', 'rhs', 'lhs_text', 'rhs_text'],
                'additionalProperties': False,
            },
        },
        'relations': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'expr': {'type': 'string'}, 'text': {'type': 'string'},
                    'transformed_from_previous': {'type': 'boolean'},
                },
                'required': ['expr', 'text', 'transformed_from_previous'],
                'additionalProperties': False,
            },
        },
        'final_answer': {'type': 'string'},
    },
    'required': ['claims', 'relations', 'final_answer'],
    'additionalProperties': False,
}


def extract_claims(llm: LLMClient, step_text: str) -> Dict[str, Any]:
    user = f'抜き出しの規則:\n{EXTRACTION_RULES}\n\n解説文:\n{step_text}'
    # 抜き出しは機械的な変換なので，思考を浅くして速くする（判定は sympy が行う）
    out = llm.generate_json(system=EXTRACTION_SYSTEM, user=user, schema=_EXTRACTION_SCHEMA,
                            schema_name='calculation_claims', temperature=0.0, thinking_level='low')
    claims = [c for c in out.get('claims', []) if isinstance(c, dict) and c.get('lhs') and c.get('rhs')]
    relations = [r for r in out.get('relations', []) if isinstance(r, dict) and r.get('expr')]
    return {'claims': claims, 'relations': relations, 'final_answer': str(out.get('final_answer', '')).strip()}


_UNIT = re.compile(r'^\s*([+-]?\d+(?:\.\d+)?(?:/\d+)?)\s*(m|cm|mm|km|g|kg|度|°|個|人|円|秒|分|時間|倍)\s*$')


def _strip_unit(answer: str) -> str:
    """「15.5 m」のような単位つきの数値から単位を外す（m を文字と読まないように）。"""
    match = _UNIT.match(answer or '')
    return match.group(1) if match else (answer or '').strip()


def merge_solution_lists(relations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """「x = 1-√6, 1+√6」のように並べた複数の解が，別々の relations として抜き出されたものを1つにまとめ直す。

    同じ文字の解いた形（x = 値）が続く場合と，解いた形のすぐ後に値だけ（1+√6）が続く場合は，それぞれの解を
    独立した解のリストとして扱い，並びの前の式とだけ解集合で比べる。解いた形から別の値の解いた形への変形は
    正しい解答には現れないので，まとめても誤りを見逃さない（不等式はまとめない）。
    """
    merged: List[Dict[str, Any]] = []
    for relation in relations:
        solved = solved_values(relation.get('expr', ''))
        last = merged[-1] if merged else None
        if solved and last is not None and last.get('_variable') and solved[0] in (None, last['_variable']):
            last['expr'] = f'{last["expr"]}, ' + ', '.join(solved[1])
            last['text'] = f'{last.get("text") or ""}, {relation.get("text") or relation.get("expr")}'
            last['merged_count'] = last.get('merged_count', 1) + 1
            continue
        entry = dict(relation)
        if solved and solved[0]:
            entry['_variable'] = solved[0]
        merged.append(entry)
    for entry in merged:
        entry.pop('_variable', None)
    return merged


def compare_with_earlier(history: List[str], relations: List[Dict[str, Any]], index: int,
                         definitions: Dict[str, str]):
    """変形して得た式（relations[index]）を，それより前の式と解集合で比べる。戻り値: (結果, 使った式の数, 比べた式)。

    - 直前の式だけでなく，それより前の式とも比べる。2次不等式を解くときの「x^2-4x+1=0 の解 x=2±√3」のような
      途中の方程式の後に「x ≦ 2-√3」と書いても，元の不等式から変形したものとして確かめられる
    - 「x ≦ 2-√3，2+√3 ≦ x」のように並べた答えが分かれて抜き出されたときは，続く解の一部（文字1つと定数だけの
      関係式）を順に合わせた和集合で比べる
    - それでも一致しなければ，前の2つの式の共通範囲（「1<m<2 と m>4/3 から 4/3<m<2」）と，合わせた範囲
      （「(i) と (ii) を合わせて」）とも比べる
    どれとも一致しなければ，直前の式と比べた結果（誤り・検算不能）を返す。
    """
    current = relations[index]
    variable = solved_piece_variable(current['expr'])
    count = 1
    if variable:
        while index + count < len(relations) and solved_piece_variable(relations[index + count]['expr']) == variable:
            count += 1
    first = None
    for used in range(1, count + 1):
        combined = ', '.join(r['expr'] for r in relations[index:index + used])
        for target in reversed(history):
            checked = check_relation_step(target, combined, definitions)
            if first is None:
                first = (checked, 1, target)
            if checked['status'] == 'ok':
                return checked, used, target
    # 前の2つの式の共通範囲（連立不等式）または合わせた範囲（場合分けのまとめ）と一致するか
    combined = ', '.join(r['expr'] for r in relations[index:index + count])
    recent = [h for h in reversed(history)][:6]
    for i, a in enumerate(recent):
        for b in recent[i + 1:]:
            for mode, word in (('and', 'かつ'), ('or', 'または')):
                checked = check_relation_combination([b, a], combined, mode, definitions)
                if checked['status'] == 'ok':
                    return checked, count, f'{b} {word} {a}'
    return first


def _is_case_redefinition(old_value: str, new_value: str, extracted: Dict[str, Any],
                          definitions: Dict[str, str]) -> bool:
    """同じ文字の2回目の式が，場合分けの別の場合の式か。

    新しい式が古い式の符号を変えたもの（絶対値を外したとき）か，このステップに場合を表す不等式の条件
    （0 < x < 2 など）があり，新しい式も文字を含む式であるとき，場合ごとの式とみなす。
    """
    if check_claim(f'-({old_value})', new_value, definitions)['status'] == 'ok':
        return True
    has_cases = any(re.search(r'[<>≦≧]|\\[lg]eq?', str(r.get('expr', ''))) for r in extracted.get('relations', []))
    return has_cases and bool(re.search(r'[A-Za-z]', str(new_value).replace('sqrt', '')))


def verify_step(step_id: int, extracted: Dict[str, Any], definitions: Dict[str, str],
                correct_answer: Optional[str] = None, previous_relation: Optional[str] = None) -> Dict[str, Any]:
    """抜き出した計算を検算する。definitions はこのステップで定義された文字で更新される。

    等式の計算（claims）に加えて，不等式・方程式の変形（relations）を直前の式と解集合で比べて検算する。
    previous_relation は前のステップの最後の関係式（ステップをまたぐ変形の検算に使う）。
    """
    # 1巡目: 文字の定義（D = b^2 - 4ac，a = 2 など）を，書かれた順序によらず先に集める
    definition_index = set()
    local_names = set()  # このステップで定義・更新した文字
    for index, claim in enumerate(extracted['claims']):
        name = symbol_name(claim['lhs'])  # b' のような文字も，定義と代入で同じ名前（b_p）にする
        if name not in definitions and check_claim(claim['lhs'], claim['rhs'], definitions)['status'] == 'definition':
            definitions[name] = str(claim['rhs']).strip()
            definition_index.add(index)
            local_names.add(name)
    # 2巡目: 定義を代入して残りの計算を検算する。同じ文字の2回目以降の式（D = 5^2-4·2(m-3) = … = 49-8m）は，
    # その文字の直前の値と比べ，成り立てば値を更新していく（D = b^2-4ac のように係数が未定義の公式でも連鎖をたどれる）
    results = []
    for index, claim in enumerate(extracted['claims']):
        name = symbol_name(claim['lhs'])
        if index in definition_index:
            results.append({**claim, 'status': 'definition', 'true_value': None,
                            'detail': f'{claim.get("lhs_text") or claim["lhs"]} の定義として記録しました'})
            continue
        if _IDENTIFIER.match(name) and name in definitions:
            checked = check_claim(definitions[name], claim['rhs'], definitions)
            if checked['status'] == 'ng' and _is_case_redefinition(definitions[name], claim['rhs'], extracted, definitions):
                # 場合分けで，同じ文字（y など）を場合ごとに別の式で表したもの（y = x^2-2x，0<x<2 では y = -(x^2-2x)）
                checked = {'status': 'definition', 'true_value': None,
                           'detail': f'場合分けの別の場合の {claim.get("lhs_text") or name} の式として記録しました'}
                definitions[name] = str(claim['rhs']).strip()
                local_names.add(name)
            elif checked['status'] == 'ng' and name not in local_names:
                # 前のステップの文字が別の量として使われている可能性があるため，誤りと断定しない
                checked = {'status': 'unverifiable', 'true_value': None,
                           'detail': f'前のステップの {name} と値が異なるため，別の量として使われている可能性があり検算しませんでした'}
            if checked['status'] != 'ng' and is_math_expression(claim['rhs']):
                definitions[name] = str(claim['rhs']).strip()
                local_names.add(name)
        else:
            checked = check_claim(claim['lhs'], claim['rhs'], definitions)
        results.append({**claim, **checked})

    feedback_lines = [
        f'ステップ{step_id}の「{r.get("lhs_text") or r["lhs"]} = {r.get("rhs_text") or r["rhs"]}」は誤りです'
        f'（正しくは {r["true_value"]}）。'
        for r in results if r['status'] == 'ng'
    ]

    # 不等式・方程式の変形: 変形して得た式だけ，それより前の式と解集合が一致するかを確かめる
    relation_results = []
    previous = previous_relation
    history = [previous_relation] if previous_relation else []  # このステップでそれまでに書かれた式
    relations = merge_solution_lists(extracted.get('relations', []))
    index = 0
    while index < len(relations):
        relation = relations[index]
        entry = {**relation, 'status': 'condition', 'detail': '新しい条件のため比較しません',
                 'previous_expr': None, 'previous_solution': None, 'current_solution': None}
        used = 1
        if relation.get('transformed_from_previous') and history:
            checked, used, target = compare_with_earlier(history, relations, index, definitions)
            entry.update(checked, previous_expr=target)
        relation_results.append(entry)
        for extra in relations[index + 1:index + used]:  # 合わせて比べた残りの部分（x ≦ 2-√3，2+√3 ≦ x の後半など）
            relation_results.append({**extra, 'status': entry['status'], 'previous_expr': entry['previous_expr'],
                                     'previous_solution': entry.get('previous_solution'),
                                     'current_solution': entry.get('current_solution'),
                                     'detail': '直前の式と合わせて1つの答えとして比べました'})
        combined = ', '.join(r['expr'] for r in relations[index:index + used])
        history.append(combined)
        previous = combined
        index += used
    for r in relation_results:
        if r['status'] == 'ng':
            feedback_lines.append(
                f'ステップ{step_id}の「{r["previous_expr"]}」から「{r.get("text") or r["expr"]}」への変形は誤りです'
                f'（変形前の解は {r["previous_solution"]}，変形後の解は {r["current_solution"]} で一致しません）。'
            )
    final_check = None
    if correct_answer:
        final_answer = _strip_unit(extracted.get('final_answer', ''))
        if final_answer:
            verdict = judge_answer(correct_answer, final_answer)
            final_check = {'final_answer': final_answer, 'correct_answer': correct_answer, **verdict}
            if not verdict['is_correct']:
                feedback_lines.append(f'結論の「{final_answer}」は正しい答え「{correct_answer}」と一致しません。')
        else:
            final_check = {'final_answer': '', 'correct_answer': correct_answer, 'is_correct': None,
                           'method': 'not_found', 'detail': '最終的な答えを抜き出せませんでした'}

    counts = {s: sum(1 for r in results if r['status'] == s) for s in ('ok', 'ng', 'definition', 'unverifiable')}
    relation_counts = {s: sum(1 for r in relation_results if r['status'] == s)
                       for s in ('ok', 'ng', 'condition', 'unverifiable')}
    return {
        'ok': not feedback_lines,
        'claims': results,
        'counts': counts,
        'relations': relation_results,
        'relation_counts': relation_counts,
        'last_relation': previous,
        'final_check': final_check,
        'feedback': '\n'.join(feedback_lines) if feedback_lines else None,
    }


# ---------------------------------------------------------------- 生成の全体（Chain 2〜4）

def _assemble(problem: Dict[str, Any], steps: List[Dict[str, Any]], texts: List[str]) -> str:
    """Chain 3: 各ステップの説明を順に結合する。"""
    parts = [f'# {problem["title"]}']
    for step, text in zip(steps, texts):
        parts.append(f'## ステップ{step["id"]}　{step["title"]}\n\n{text}')
    return '\n\n'.join(parts) + '\n'


def generate_explanation(llm: LLMClient, problem: Dict[str, Any], node_states: Optional[Dict[str, Any]] = None,
                         error_tendencies: Optional[Dict[str, int]] = None, kind: str = 'individual') -> Dict[str, Any]:
    """積み上げ型の個別解説（kind='individual'）またはマスター解説（kind='master'）を生成する。

    テンプレート（problem['template']）がある問題では，マスター解説はテンプレートそのもの（LLM は呼ばない）で，
    個別の解説はテンプレートの各手順の本文だけを書き直して，同じ体裁の TeX に組み立てる。
    """
    steps = problem['steps']
    if not steps:
        raise ExplanationError('解答ステップがありません（先に Chain 1 を実行してください）')
    template = problem.get('template')
    bodies = tex_template.step_bodies(template) if template else []
    if template and len(bodies) != len(steps):
        raise ExplanationError('テンプレートの手順の数と解答ステップの数が一致しません（テンプレートを読み込み直してください）')
    if template and kind == 'master':
        return template_master(problem)
    if kind == 'master':
        decisions = [{'step_id': s['id'], 'title': s['title'], 'nodes': s.get('nodes', []), **master_decision(s)}
                     for s in steps]
    else:
        decisions = plan_explanation(steps, node_states or {}, error_tendencies or {})

    definitions: Dict[str, str] = {}
    last_relation: Optional[str] = None
    texts: List[str] = []
    step_traces = []
    for index, (step, decision) in enumerate(zip(steps, decisions)):
        text, attempts, verification, definitions, last_relation = _write_until_verified(
            llm, problem, steps, index, decision, bodies, texts, definitions, last_relation)
        texts.append(text)
        step_traces.append(_step_trace(step, decision, text, attempts, verification))
    result = _result(kind, problem, steps, texts, step_traces, getattr(llm, 'model_name', None))
    return _polished(llm, problem, result) if template and kind == 'individual' else result


def _polished(llm: Optional[LLMClient], problem: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """書き足した部分の文体（敬体）・注意の重複や長さが残っていれば，1回の呼び出しでまとめて整える。"""
    try:
        polished = polish.polish(llm, problem, result)
        if polished['result'] is not None:
            return polished['result']
    except Exception:  # 整えられなくても，検算済みの解説はそのまま使う
        pass
    return result


def _write(llm: LLMClient, problem: Dict[str, Any], steps: List[Dict[str, Any]], index: int, decision: Dict[str, Any],
           bodies: List[str], texts: List[str], feedback: Optional[str]) -> str:
    if problem.get('template'):
        return write_template_step(llm, problem, steps, steps[index], decision, bodies[index], texts, feedback)
    return write_step(llm, problem, steps, steps[index], decision, texts, feedback)


def _verify_text(problem: Dict[str, Any], steps: List[Dict[str, Any]], index: int, text: str,
                 extracted: Optional[Dict[str, Any]], definitions: Dict[str, str],
                 last_relation: Optional[str], extract_error: Optional[Exception] = None) -> Dict[str, Any]:
    """抜き出した計算（extracted）を検算する（Chain 4。sympy だけで LLM は呼ばない）。definitions は更新される。"""
    template = problem.get('template')
    # テンプレートの問題は答をテンプレートの「答」で示すので，最後の手順の結論は照合しない
    final_answer = problem.get('correct_answer') if index == len(steps) - 1 and not template else None
    if extracted is None:  # 抜き出しの失敗は検算できなかったものとして扱う
        verification = {'ok': True, 'claims': [], 'counts': {}, 'relations': [], 'relation_counts': {},
                        'last_relation': last_relation, 'final_check': None, 'feedback': None,
                        'error': f'計算の抜き出しに失敗しました（{extract_error.__class__.__name__}）'}
    else:
        verification = verify_step(steps[index]['id'], extracted, definitions, final_answer, last_relation)
    if template:
        latex_problems = tex_template.check_latex(text)
        if latex_problems:  # 壊れた TeX は PDF にできないので，計算の誤りと同じく差し戻す
            verification = {**verification, 'ok': False, 'latex_errors': latex_problems,
                            'feedback': 'TeX の書き方に誤りがあります: ' + '，'.join(latex_problems)
                                        + (f'。{verification["feedback"]}' if verification.get('feedback') else '')}
    return verification


def _write_until_verified(llm: LLMClient, problem: Dict[str, Any], steps: List[Dict[str, Any]], index: int,
                          decision: Dict[str, Any], bodies: List[str], texts: List[str], definitions: Dict[str, str],
                          last_relation: Optional[str], feedback: Optional[str] = None,
                          attempts: Optional[List[Dict[str, Any]]] = None):
    """1ステップを書き，検算が通るまで差し戻して書き直す（上限 MAX_ATTEMPTS 回）。"""
    attempts = list(attempts or [])
    text, verification = '', {}
    start = len(attempts) + 1
    for attempt in range(start, start + MAX_ATTEMPTS):
        text = _write(llm, problem, steps, index, decision, bodies, texts, feedback)
        step_definitions = dict(definitions)
        try:
            extracted = extract_claims(llm, tex_template.restore_figures(text, {}) if problem.get('template') else text)
            verification = _verify_text(problem, steps, index, text, extracted, step_definitions, last_relation)
        except Exception as exc:
            verification = _verify_text(problem, steps, index, text, None, step_definitions, last_relation, exc)
        attempts.append({'attempt': attempt, 'text': text, **verification})
        if verification['ok']:
            return text, attempts, verification, step_definitions, verification.get('last_relation')
        feedback = verification['feedback']
    return text, attempts, verification, definitions, last_relation


def _step_trace(step: Dict[str, Any], decision: Dict[str, Any], text: str, attempts: List[Dict[str, Any]],
                verification: Dict[str, Any]) -> Dict[str, Any]:
    step_status = 'ok' if verification.get('ok') else 'needs_review'
    step_warnings = []
    checked_relations = [r for r in verification.get('relations', []) if r['status'] in ('ok', 'ng')]
    if not verification.get('claims') and not checked_relations:
        step_warnings.append(f'ステップ{step["id"]}: 計算・式の変形が一つも抜き出されなかったため，このステップは検算されていません')
    if verification.get('error'):
        step_warnings.append(f'ステップ{step["id"]}: {verification["error"]}')
    if step_status == 'needs_review':
        step_warnings.append(f'ステップ{step["id"]}: 再生成の上限（{MAX_ATTEMPTS}回）に達したため，教員の確認が必要です')
    return {
        'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
        'decision': decision, 'text': text, 'status': step_status,
        'attempt_count': len(attempts), 'attempts': attempts, 'warnings': step_warnings,
    }


def _result(kind: str, problem: Dict[str, Any], steps: List[Dict[str, Any]], texts: List[str],
            step_traces: List[Dict[str, Any]], model_name: Optional[str]) -> Dict[str, Any]:
    template = problem.get('template')
    return {
        'kind': kind,
        'status': 'ok' if all(s['status'] == 'ok' for s in step_traces) else 'needs_review',
        'content': tex_template.assemble(template, texts) if template else _assemble(problem, steps, texts),
        'format': 'latex' if template else 'markdown',
        'steps': step_traces,
        'warnings': [w for s in step_traces for w in s['warnings']],
        'model_name': model_name,
    }


# ---------------------------------------------------------------- 要確認の解説の手直し（API の呼び出しを最小にする）

def _extracted_from_attempt(attempt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """記録に残っている「抜き出した計算」を，もう一度検算できる形に戻す（LLM を呼ばずに再検算するため）。"""
    if attempt.get('error') and not attempt.get('claims') and not attempt.get('relations'):
        return None
    return {
        'claims': [{k: c.get(k, '') for k in ('lhs', 'rhs', 'lhs_text', 'rhs_text')} for c in attempt.get('claims', [])],
        'relations': [{'expr': r['expr'], 'text': r.get('text') or r['expr'],
                       'transformed_from_previous': bool(r.get('transformed_from_previous'))}
                      for r in attempt.get('relations', [])],
        'final_answer': (attempt.get('final_check') or {}).get('final_answer', ''),
    }


def repair_explanation(llm: Optional[LLMClient], problem: Dict[str, Any], previous: Dict[str, Any],
                       dry_run: bool = False) -> Dict[str, Any]:
    """要確認の解説を，なるべく LLM を呼ばずに直す。

    1. 各ステップの最後の文章と，記録に残っている「抜き出した計算」を，現在の検算（sympy）でもう一度確かめる。
       検算の不具合が直っていればここで通り，LLM は呼ばない。
    2. それでも通らないステップだけを，その指摘を添えて書き直す（他のステップの文章と判断はそのまま）。
    dry_run=True のときは書き直さず，書き直しが必要なステップを数えるだけ。
    戻り値: {'result': 新しい解説 or None, 'rechecked_ok': [...], 'rewrite_steps': [...]}
    """
    steps = problem['steps']
    old_steps = previous.get('steps', [])
    template = problem.get('template')
    bodies = tex_template.step_bodies(template) if template else []
    if len(old_steps) != len(steps) or [s.get('step_id') for s in old_steps] != [s['id'] for s in steps]:
        raise ExplanationError('解答ステップが生成時から変わっているため，手直しではなく生成し直してください')
    definitions: Dict[str, str] = {}
    last_relation: Optional[str] = None
    texts: List[str] = []
    traces = []
    rewrite_steps, rechecked_ok = [], []
    for index, (step, old) in enumerate(zip(steps, old_steps)):
        decision, text = old['decision'], old.get('text', '')
        attempts = list(old.get('attempts', []))
        last_attempt = attempts[-1] if attempts else None
        step_definitions = dict(definitions)
        if last_attempt is None:  # 検算の記録がない（テンプレートのまま等）
            verification = {'ok': True, 'claims': [], 'relations': [], 'last_relation': last_relation}
        else:
            extracted = _extracted_from_attempt(last_attempt)
            verification = _verify_text(problem, steps, index, text, extracted, step_definitions, last_relation,
                                        RuntimeError('記録なし') if extracted is None else None)
        if verification['ok']:
            if old.get('status') != 'ok':
                rechecked_ok.append(step['id'])
                attempts.append({'attempt': len(attempts) + 1, 'text': text, 'rechecked': True, **verification})
            definitions, last_relation = step_definitions, verification.get('last_relation', last_relation)
            texts.append(text)
            traces.append(_step_trace(step, decision, text, attempts, verification) if last_attempt
                          else {**old, 'status': 'ok'})
            continue
        rewrite_steps.append(step['id'])
        if dry_run:
            texts.append(text)
            traces.append(old)
            continue
        text, attempts, verification, definitions, last_relation = _write_until_verified(
            llm, problem, steps, index, decision, bodies, texts, definitions, last_relation,
            feedback=verification['feedback'], attempts=attempts)
        texts.append(text)
        traces.append(_step_trace(step, decision, text, attempts, verification))
    if dry_run:
        return {'result': None, 'rechecked_ok': rechecked_ok, 'rewrite_steps': rewrite_steps}
    model_name = (getattr(llm, 'model_name', None) if rewrite_steps else None) or previous.get('model_name')
    kind = previous.get('kind', 'individual')
    result = _result(kind, problem, steps, texts, traces, model_name)
    if rewrite_steps and template and kind == 'individual':  # 書き直した手順も，生成のときと同じように整える
        result = _polished(llm, problem, result)
    return {'result': result, 'rechecked_ok': rechecked_ok, 'rewrite_steps': rewrite_steps}


def template_master(problem: Dict[str, Any]) -> Dict[str, Any]:
    """テンプレートがある問題のマスター解説（比較用 (A)）: 教員のテンプレートそのもの。LLM は呼ばない。"""
    template = problem['template']
    bodies = tex_template.step_bodies(template)
    traces = []
    for step, body in zip(problem['steps'], bodies):
        decision = {'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []), **master_decision(step)}
        decision['reasons'] = ['マスター解説: 教員が作ったテンプレートの手順をそのまま使う']
        traces.append({'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
                       'decision': decision, 'text': body, 'status': 'ok', 'attempt_count': 0, 'attempts': [],
                       'warnings': []})
    return {'kind': 'master', 'status': 'ok', 'content': template['tex'], 'format': 'latex', 'steps': traces,
            'warnings': [], 'model_name': 'teacher-template'}


def template_fallback(problem: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """個別化の生成が失敗し続けたときに代わりに載せる解説: テンプレートの手順をそのまま使う（検算済みの教員の解説）。"""
    result = template_master(problem)
    result['kind'] = 'individual'
    result['warnings'] = [f'個別化できなかったため，マスター解説の手順をそのまま載せた（{reason[:200]}）']
    return result


def template_common(problem: Dict[str, Any]) -> Dict[str, Any]:
    """テンプレートに手順のない問題の全員共通の解説: テンプレートの部分をそのまま配る。"""
    decision = {'elements': ['result'], 'weakest_node': None, 'confidence': None, 'evidence_count': None,
                'node_confidences': {}, 'cautions': [],
                'reasons': ['テンプレートに手順がないため，教員のテンプレートをそのまま全員に配る']}
    text = problem['template']['tex']
    trace = {'step_id': 1, 'title': '全員共通の解説（テンプレート）', 'nodes': [], 'decision': decision, 'text': text,
             'status': 'ok', 'attempt_count': 0, 'attempts': [], 'warnings': []}
    return {'kind': 'common', 'status': 'ok', 'content': text, 'format': 'latex', 'steps': [trace],
            'warnings': [], 'model_name': 'teacher-template'}


# 全員共通の解説の書き方。教員から参考として示された市販の解答解説集（チャート式の解答編）の構成と文体を，
# 要素ごとに言い直したもの（原文は引用しない）。書き方を変えたら COMMON_STYLE_VERSION を上げると，
# 既にある共通の解説が次の生成で作り直される。
COMMON_STYLE_VERSION = 2

COMMON_SYSTEM = (
    'あなたは高等学校数学の定期試験の解答解説を書きます。この解説は生徒によらず全員に配る共通のものです。'
    '参考書の解答編のような，簡潔で筋道の明確な解説を，次の構成と書き方の規則に従って Markdown で書きます。'
)

COMMON_STYLE = """構成（この順に書く。【】の部分は不要なら省く）
1. 方針の枠: 「> **指針**　」で始め，何に着目してどの手順で解くかを1〜3文で示す。続けて
   「> **CHART**　」で始め，この問題から引き出せる解き方の原則を短い標語で1行書く（例: 絶対値は中身の符号で場合分け）。
2. 「### 解答」の見出しの下に解答を書く。小問があれば (1)，(2) と行頭に番号を付けて順に解く。
3. 【「### 別解」】見方の異なる解き方が短く書けるときだけ。
4. 【「### 検討」】間違えやすい点，結果の確かめ方，他の問題への広げ方を1〜3文で。

解答の書き方
- 常体（である調）で簡潔に書く。「〜から」「よって」「ゆえに」「したがって」「以上から」で式と式をつなぎ，
  説明の文は短くする。
- 式は $...$ で囲み，1行に1つの式変形を書く。計算は「左辺 = 右辺」の形で途中を省略しない。
- 後で使う式には行末に「……①」「……②」と番号を付け，「①，②を解いて」のように参照する。
- 場合分けは [1]，[2]，[3] と番号を付けて行頭に置き，各場合の条件を先に書く。最後に「以上から」でまとめる。
- 求めた値が場合分けや問題の条件を満たすかを確かめ，「これは〜を満たす」「これは〜を満たさないから不適」と書く。
- 式変形の理由や使った公式など，本文の流れから外れる補足は，その行の直後に「← 」で始まる短い1行で書く
  （参考書の右欄の注記に当たる）。
- グラフや図をかく問題では図を描けないので，かき方の手順（式の変形，場合分けの区間ごとの式）と，
  グラフの形・頂点・軸・座標軸との交点・折れ曲がる点など，図に書き込むべき特徴を式と言葉で示す。
- 求める答えは太字（**...**）で示し，解答の最後の行に「答え: ...」の形で結論をまとめる。"""

COMMON_EXAMPLE = """書き方の例（別の問題の解説。内容ではなく構成と文体だけを参考にする）
問題: 2次関数 $y = x^2 - 6x + 5$ のグラフの頂点と，$x$ 軸との共有点の座標を求めよ。

> **指針**　頂点は平方完成して $y = (x-p)^2 + q$ の形から読み取る。$x$ 軸との共有点は $y = 0$ とおいた2次方程式の実数解である。
> **CHART**　頂点は平方完成，$x$ 軸との共有点は $y=0$

### 解答
$y = x^2 - 6x + 5 = (x-3)^2 - 9 + 5 = (x-3)^2 - 4$
← $x^2 - 6x = (x-3)^2 - 9$
よって，頂点は **点 $(3, -4)$**
$x$ 軸との共有点の $x$ 座標は，$x^2 - 6x + 5 = 0$ の解である。……①
①から $(x-1)(x-5) = 0$　ゆえに　$x = 1, 5$
したがって，共有点の座標は **$(1, 0)$，$(5, 0)$**
答え: 頂点 $(3, -4)$，共有点 $(1, 0), (5, 0)$

### 検討
頂点の $y$ 座標が負で下に凸であるから，グラフは $x$ 軸と2点で交わる。共有点が頂点の $x$ 座標 $3$ について対称であることも確かめになる。"""


def write_common(llm: LLMClient, problem: Dict[str, Any], feedback: Optional[str] = None) -> str:
    answer = problem.get('correct_answer') or '（模範解答の答えは登録されていません）'
    user = (f'{COMMON_STYLE}\n\n{COMMON_EXAMPLE}\n\n---\n'
            f'問題文:\n{problem["problem_text"]}\n\n模範解答の答え: {answer}\n\n'
            'この問題の解説を，上の構成と書き方の規則に従って書いてください。')
    if feedback:
        user += f'\n\n前回書いた解説には次の誤りがありました。誤りを直して，解説を書き直してください。\n{feedback}'
    return llm.generate_text(system=COMMON_SYSTEM, user=user, temperature=0.3).strip()


def repair_common(llm: Optional[LLMClient], problem: Dict[str, Any], previous: Dict[str, Any],
                  dry_run: bool = False) -> Dict[str, Any]:
    """要確認の全員共通の解説（解答ステップがない問題）を直す。記録の計算を再検算して通れば LLM を呼ばない。"""
    old = (previous.get('steps') or [{}])[0]
    attempts = list(old.get('attempts', []))
    extracted = _extracted_from_attempt(attempts[-1]) if attempts else None
    if extracted is not None:
        verification = verify_step(1, extracted, {}, problem.get('correct_answer') or None, None)
        if verification['ok']:
            attempts.append({'attempt': len(attempts) + 1, 'text': old.get('text', ''), 'rechecked': True, **verification})
            warnings = [w for w in old.get('warnings', []) if '上限' not in w]
            trace = {**old, 'status': 'ok', 'attempts': attempts, 'attempt_count': len(attempts), 'warnings': warnings}
            return {'result': {**previous, 'status': 'ok', 'steps': [trace], 'warnings': warnings},
                    'rechecked_ok': [1], 'rewrite_steps': []}
    if dry_run:
        return {'result': None, 'rechecked_ok': [], 'rewrite_steps': [1]}
    return {'result': generate_common_explanation(llm, problem), 'rechecked_ok': [], 'rewrite_steps': [1]}


def generate_common_explanation(llm: LLMClient, problem: Dict[str, Any]) -> Dict[str, Any]:
    """解答ステップがない問題（Chain 1 で分解できなかった問題）の，個別化しない全員共通の解説。

    生徒の定着度による判断（Chain 2）はできないため全員に同じ解説を配るが，解説の計算の検算（Chain 4）と，
    上限を超えたら教員の確認に回す流れは個別の解説と同じにする。
    """
    feedback = None
    attempts = []
    text = ''
    verification: Dict[str, Any] = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = write_common(llm, problem, feedback)
        try:
            extracted = extract_claims(llm, text)
            verification = verify_step(1, extracted, {}, problem.get('correct_answer') or None, None)
        except Exception as exc:
            verification = {'ok': True, 'claims': [], 'counts': {}, 'relations': [], 'relation_counts': {},
                            'last_relation': None, 'final_check': None, 'feedback': None,
                            'error': f'計算の抜き出しに失敗しました（{exc.__class__.__name__}）'}
        attempts.append({'attempt': attempt, 'text': text, **verification})
        if verification['ok']:
            break
        feedback = verification['feedback']
    status = 'ok' if verification.get('ok') else 'needs_review'
    warnings = []
    checked_relations = [r for r in verification.get('relations', []) if r['status'] in ('ok', 'ng')]
    if not verification.get('claims') and not checked_relations:
        warnings.append('計算・式の変形が一つも抜き出されなかったため，この解説は検算されていません')
    if verification.get('error'):
        warnings.append(verification['error'])
    if status == 'needs_review':
        warnings.append(f'再生成の上限（{MAX_ATTEMPTS}回）に達したため，教員の確認が必要です')
    decision = {'elements': ['result', 'reasoning'], 'weakest_node': None, 'confidence': None, 'evidence_count': None,
                'node_confidences': {}, 'cautions': [],
                'reasons': ['解答ステップ（Chain 1）がないため，個別化しない全員共通の解説を書く']}
    trace = {'step_id': 1, 'title': '全員共通の解説', 'nodes': [], 'decision': decision, 'text': text,
             'status': status, 'attempt_count': len(attempts), 'attempts': attempts, 'warnings': warnings}
    return {'kind': 'common', 'status': status, 'content': f'# {problem["title"]}\n\n{text}\n',
            'steps': [trace], 'warnings': warnings, 'model_name': getattr(llm, 'model_name', None)}


# ---------------------------------------------------------------- 比較の指標（5.5節 表10）

def text_similarity(a: str, b: str) -> float:
    """文字列の一致度（difflib）。"""
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 3)


def cosine_similarity(a: List[float], b: List[float]) -> Optional[float]:
    """埋め込みベクトルのコサイン類似度（意味の類似度）。"""
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b or len(a) != len(b):
        return None
    return round(sum(x * y for x, y in zip(a, b)) / (norm_a * norm_b), 3)


def explanation_metrics(trace: Dict[str, Any], node_states: Dict[str, Any]) -> Dict[str, Any]:
    """未定着の箇所を補えているか・定着済みの箇所が冗長でないかを，ステップと前提知識の対応から算出する。"""
    weak_steps, mastered_steps = [], []
    for step in trace['steps']:
        nodes = step.get('nodes') or []
        if not nodes:
            continue
        if any(confidence_of(node_states, n)[0] < DETAIL_THRESHOLD for n in nodes):
            weak_steps.append(step)
        else:
            mastered_steps.append(step)
    detailed = [s for s in weak_steps if 'reasoning' in s['decision']['elements'] or s.get('kept_from_master')]
    return {
        'weak_step_count': len(weak_steps),
        'weak_steps_detailed_ratio': round(len(detailed) / len(weak_steps), 3) if weak_steps else None,
        'mastered_step_count': len(mastered_steps),
        'mastered_steps_chars': sum(len(s['text']) for s in mastered_steps),
        'total_chars': sum(len(s['text']) for s in trace['steps']),
    }
