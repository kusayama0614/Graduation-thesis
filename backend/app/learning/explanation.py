"""フェーズB: 積み上げ型の解説生成（卒業論文 3.4節・3.5節，表5）

Chain 1  問題文を誰にとっても省略できない解答ステップに分解し，各ステップが依存する前提知識ノードを
         付ける（LLM。出力は JSON Schema でノード名の選択肢に制約）。1問につき1回だけ実行する。
Chain 2  ステップごとに，生徒の定着度から「含める要素」をプログラムの条件分岐で決め（LLM は呼ばない），
         その方針で LLM が説明を一から書く。
Chain 3  各ステップの説明を順に結合する（プログラム）。
Chain 4  説明中の「左辺 = 右辺」の計算を LLM が式として抜き出し，sympy で検算する。誤りがあれば
         どの式が誤りで正しい値は何かを添えて Chain 2 に差し戻す。上限を超えたら教員の確認に回す。

詳しさの決定（3.4節の方式(1)）: ステップが依存するノードのうち最も定着度の低いものの値 c に応じて，
含める要素を切り替える。閾値は ELEMENT_RULES に集約しており，判断の理由は全て trace に残る。
"""
from __future__ import annotations

import difflib
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from app.learning import graph
from app.learning.llm import LLMClient
from app.learning.profile import confidence_of
from app.utils.math_verification import _IDENTIFIER, check_claim, check_relation_step, is_math_expression, judge_answer

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
    return llm.generate_text(system=CHAIN2_SYSTEM, user=user, temperature=0.3).strip()


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
    '- 文字は，解説中で文字として書かれている記号（D，m，x など）だけを使う。「塔全体の高さ」のように言葉で書かれた量に'
    '自分で文字を割り当ててはいけない。その場合は数値の計算だけを抜き出す'
    '（例:「塔全体の高さ = 14.004 + 1.5 = 15.504」→ lhs「14.004 + 1.5」，rhs「15.504」）。\n'
    '- relations には，不等式と，文字について解く方程式（「(m+6)(m-2)=0」「m=2, -6」など）を書かれた順に全て入れる。'
    'expr は sympy で解釈できる表記，text は元の表記とする。その式が直前の relations の式を変形して得られたもの'
    '（移項・両辺を割る・因数分解して解くなど）なら transformed_from_previous を true，新しい条件や別の条件を'
    '書き始めたもの（D > 0 という条件を立てる，別の条件を並べる など）なら false にする。'
    'このステップの最初の式が，前のステップの最後の式を変形したものなら true にする。\n'
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
        name = str(claim['lhs']).strip()
        if name not in definitions and check_claim(claim['lhs'], claim['rhs'], definitions)['status'] == 'definition':
            definitions[name] = str(claim['rhs']).strip()
            definition_index.add(index)
            local_names.add(name)
    # 2巡目: 定義を代入して残りの計算を検算する。同じ文字の2回目以降の式（D = 5^2-4·2(m-3) = … = 49-8m）は，
    # その文字の直前の値と比べ，成り立てば値を更新していく（D = b^2-4ac のように係数が未定義の公式でも連鎖をたどれる）
    results = []
    for index, claim in enumerate(extracted['claims']):
        name = str(claim['lhs']).strip()
        if index in definition_index:
            results.append({**claim, 'status': 'definition', 'true_value': None,
                            'detail': f'{name} の定義として記録しました'})
            continue
        if _IDENTIFIER.match(name) and name in definitions:
            checked = check_claim(definitions[name], claim['rhs'], definitions)
            if checked['status'] == 'ng' and name not in local_names:
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

    # 不等式・方程式の変形: 直前の式を変形したものだけ，解集合が一致するかを確かめる
    relation_results = []
    previous = previous_relation
    for relation in extracted.get('relations', []):
        entry = {**relation, 'status': 'condition', 'detail': '新しい条件のため比較しません',
                 'previous_expr': None, 'previous_solution': None, 'current_solution': None}
        if relation.get('transformed_from_previous') and previous:
            entry.update(check_relation_step(previous, relation['expr'], definitions), previous_expr=previous)
        relation_results.append(entry)
        previous = relation['expr']
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
    """積み上げ型の個別解説（kind='individual'）またはマスター解説（kind='master'）を生成する。"""
    steps = problem['steps']
    if not steps:
        raise ExplanationError('解答ステップがありません（先に Chain 1 を実行してください）')
    if kind == 'master':
        decisions = [{'step_id': s['id'], 'title': s['title'], 'nodes': s.get('nodes', []), **master_decision(s)}
                     for s in steps]
    else:
        decisions = plan_explanation(steps, node_states or {}, error_tendencies or {})

    definitions: Dict[str, str] = {}
    last_relation: Optional[str] = None
    texts: List[str] = []
    step_traces = []
    warnings: List[str] = []
    for index, (step, decision) in enumerate(zip(steps, decisions)):
        is_last = index == len(steps) - 1
        feedback = None
        attempts = []
        text = ''
        verification: Dict[str, Any] = {}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            text = write_step(llm, problem, steps, step, decision, texts, feedback)
            step_definitions = dict(definitions)
            try:
                extracted = extract_claims(llm, text)
                verification = verify_step(step['id'], extracted, step_definitions,
                                           problem.get('correct_answer') if is_last else None, last_relation)
            except Exception as exc:  # 抜き出しの失敗は検算できなかったものとして扱う
                extracted = {'claims': [], 'relations': [], 'final_answer': ''}
                verification = {'ok': True, 'claims': [], 'counts': {}, 'relations': [], 'relation_counts': {},
                                'last_relation': last_relation, 'final_check': None, 'feedback': None,
                                'error': f'計算の抜き出しに失敗しました（{exc.__class__.__name__}）'}
            attempts.append({'attempt': attempt, 'text': text, **verification})
            if verification['ok']:
                definitions = step_definitions
                last_relation = verification.get('last_relation')
                break
            feedback = verification['feedback']
        step_status = 'ok' if verification.get('ok') else 'needs_review'
        step_warnings = []
        checked_relations = [r for r in verification.get('relations', []) if r['status'] in ('ok', 'ng')]
        if not verification.get('claims') and not checked_relations:
            step_warnings.append(f'ステップ{step["id"]}: 計算・式の変形が一つも抜き出されなかったため，このステップは検算されていません')
        if verification.get('error'):
            step_warnings.append(f'ステップ{step["id"]}: {verification["error"]}')
        if step_status == 'needs_review':
            step_warnings.append(f'ステップ{step["id"]}: 再生成の上限（{MAX_ATTEMPTS}回）に達したため，教員の確認が必要です')
        warnings.extend(step_warnings)
        texts.append(text)
        step_traces.append({
            'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
            'decision': decision, 'text': text, 'status': step_status,
            'attempt_count': len(attempts), 'attempts': attempts, 'warnings': step_warnings,
        })

    return {
        'kind': kind,
        'status': 'ok' if all(s['status'] == 'ok' for s in step_traces) else 'needs_review',
        'content': _assemble(problem, steps, texts),
        'steps': step_traces,
        'warnings': warnings,
        'model_name': getattr(llm, 'model_name', None),
    }


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


def build_subtractive(problem: Dict[str, Any], master: Dict[str, Any], node_states: Dict[str, Any],
                      error_tendencies: Dict[str, int]) -> Dict[str, Any]:
    """引き算型の解説（比較用 (B)）: マスター解説から，定着済みのステップを結果だけに縮める。LLM は呼ばない。"""
    steps = problem['steps']
    master_texts = {s['step_id']: s['text'] for s in master['steps']}
    decisions = plan_explanation(steps, node_states, error_tendencies)
    texts, traces = [], []
    for step, decision in zip(steps, decisions):
        keep = 'reasoning' in decision['elements']
        text = master_texts.get(step['id'], '') if keep else (step.get('result') or '（省略）')
        texts.append(text)
        traces.append({'step_id': step['id'], 'title': step['title'], 'nodes': step.get('nodes', []),
                       'decision': decision, 'text': text, 'status': 'ok',
                       'kept_from_master': keep, 'attempt_count': 0, 'attempts': [], 'warnings': []})
    return {'kind': 'subtractive', 'status': master['status'], 'content': _assemble(problem, steps, texts),
            'steps': traces, 'warnings': [], 'model_name': master.get('model_name')}


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
