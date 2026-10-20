"""数学的検証エンジン

採点時に「教師の解答キー」と「生徒の解答」が数学的に同値かを判定する。

卒論プロトタイプ(verify_chain.py)の層構造を、特定プリントに依存しない形で汎用化したもの:
  層1 正解の生成   : 教師が入力した解答キー(期待値)をsympyの式として解釈する(決定的)
  層2 正規化       : 全角半角・√・°・上付き文字・≦≧≠などを統一表記に直す
  層3 判定         : sympyで同値性を判定する(決定的)
  層4 結果の返却   : {'is_correct', 'method', 'detail'} を返す

sympyで解釈できない解答(選択式・自由記述など)は、従来どおり大文字小文字・全角半角を
正規化した文字列一致にフォールバックする。LLMによる確率的な正規化(層2)は含まない。
"""
from __future__ import annotations

import os
import re
import unicodedata
import warnings
from typing import Any, Dict, List, Optional, Tuple

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

MAX_LENGTH = 200
MAX_EXPONENT = 1000

_TRANSFORMATIONS = standard_transformations + (
    implicit_multiplication_application,
    convert_xor,
)

# parse_expr(eval) に渡す名前空間。未登録の名前は Symbol / Function として扱われる。
_ALLOWED_NAMES: Dict[str, Any] = {
    'Symbol': sp.Symbol,
    'Function': sp.Function,
    'Integer': sp.Integer,
    'Float': sp.Float,
    'Rational': sp.Rational,
    'Add': sp.Add,
    'Mul': sp.Mul,
    'Pow': sp.Pow,
    'sqrt': sp.sqrt,
    'sin': sp.sin,
    'cos': sp.cos,
    'tan': sp.tan,
    'log': sp.log,
    'ln': sp.log,
    'exp': sp.exp,
    'abs': sp.Abs,
    'pi': sp.pi,
}

_FORBIDDEN_CHARS = set('[]{}"\';:\\@#$&`|~?!')
_FORBIDDEN_WORDS = re.compile(
    r'\b(import|lambda|for|in|if|else|is|not|and|or|exec|eval|class|def|while|with|'
    r'yield|del|global|assert|try|raise|from|as|return|pass|None|True|False)\b'
)
_ATTRIBUTE_ACCESS = re.compile(r'\.\s*[A-Za-z_]')
_ALLOWED_AFTER_PREPROCESS = re.compile(r'^[0-9A-Za-z+\-*/^().,=<>! _±]*$')
_RELATION_SPLIT = re.compile(r'(<=|>=|!=|==|=|<|>)')
_DECIMAL_LITERAL = re.compile(r'^[+-]?\d+\.(\d+)$')

_SUPERSCRIPT_RUN = re.compile('[⁻⁰¹²³⁴⁵⁶⁷⁸⁹]+')
_SUPERSCRIPT_MAP = str.maketrans('⁻⁰¹²³⁴⁵⁶⁷⁸⁹', '-0123456789')
_CHAR_MAP = str.maketrans({
    '−': '-', '–': '-', '—': '-', '―': '-', '‐': '-',
    '×': '*', '⋅': '*', '·': '*', '・': '*', '∙': '*', '÷': '/',
    '≦': '<=', '≤': '<=', '≧': '>=', '≥': '>=', '≠': '!=',
    'π': 'pi', '、': ',', '，': ',',
})


# sympy の計算（解集合・簡約）には，入れ子の絶対値のように終わらないものがある。検算と採点の入口（check_claim・
# check_relation_step・judge_answer）は別プロセス（app/utils/sympy_isolation.py）で実行し，1回の上限の秒数を
# 超えたら，そのプロセスごと止めて「判定できなかった」ものとして扱う（スレッドは外から止められないため）
TIME_LIMIT = float(os.getenv('SYMPY_TIME_LIMIT', '10'))


class _Unparseable(Exception):
    """sympyで安全に解釈できない入力。文字列一致にフォールバックする。"""


# ---------------------------------------------------------------- 正規化 (層2)

_LATEX_COMMANDS = [
    (r'\\left|\\right', ''), (r'\\times|\\cdot', '*'), (r'\\div', '/'),
    (r'\\leqq?|\\le\b', '<='), (r'\\geqq?|\\ge\b', '>='), (r'\\neq?\b', '!='),
    (r'\\pi\b', 'pi'), (r'\^\{?\\circ\}?', '°'), (r'\\(sin|cos|tan|log|ln)\b', r'\1'),
    (r'\\q?quad\b', ' '), (r'\\[,;:>! ]', ' '),
    (r'\\pm\b|\\mp\b', '±'),
]

# 「b'」「b''」のようなプライム付きの文字（微分ではなく別の文字）。「'」は安全のため受け付けないので，
# 先に b_p・b_pp という名前に置き換える（表示するときは b' に戻す）
_PRIME = re.compile(r"([A-Za-z])(['′’]+)")
_PRIME_NAME = re.compile(r'\b([A-Za-z])_(p+)\b')


def _replace_primes(text: str) -> str:
    return _PRIME.sub(lambda m: f'{m.group(1)}_{"p" * len(m.group(2))}', text)


def _restore_primes(text: str) -> str:
    return _PRIME_NAME.sub(lambda m: m.group(1) + "'" * len(m.group(2)), text)


def symbol_name(text: Any) -> str:
    """解説中の文字の名前（D，b'，m など）を，定義の記録と代入で使う名前にそろえる。"""
    return _delatex(str(text).strip()).strip()


def _unwrap_sympy_eq(text: str) -> str:
    """LLM が sympy の書き方で返す「Eq(左辺, 右辺)」を「左辺 = 右辺」に直す。"""
    stripped = text.strip()
    if not (stripped.startswith('Eq(') and stripped.endswith(')')):
        return text
    inner, depth = stripped[3:-1], 0
    for index, ch in enumerate(inner):
        depth += ch == '('
        depth -= ch == ')'
        if ch == ',' and depth == 0:
            return f'{inner[:index].strip()} = {inner[index + 1:].strip()}'
    return text


def _delatex(text: str) -> str:
    """LLM が返しがちな LaTeX 表記（\\frac{49}{8} など）や Eq(...) を通常の式の表記に直す。"""
    text = _replace_primes(_unwrap_sympy_eq(text))
    if '\\' not in text and '$' not in text:
        return text
    text = text.replace('$', '')
    for _ in range(10):  # 入れ子の \frac / \sqrt を内側から順に置き換える
        replaced = re.sub(r'\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}', r'((\1)/(\2))', text)
        # 前に空白を入れ，直前の \pm・\le などの命令とつながらないようにする（\pm\sqrt6 → \pm sqrt(6)）
        replaced = re.sub(r'\\sqrt\{([^{}]*)\}', r' sqrt(\1)', replaced)
        # 中括弧のない \sqrt6・\sqrt 2・\sqrt x（LaTeX と同じく，次の1文字だけが根号の中に入る）
        replaced = re.sub(r'\\sqrt\s*([0-9A-Za-z])', r' sqrt(\1)', replaced)
        if replaced == text:
            break
        text = replaced
    for pattern, repl in _LATEX_COMMANDS:
        text = re.sub(pattern, repl, text)
    return text.replace('{', '(').replace('}', ')').strip()


def _normalize_text(value: Any) -> str:
    """文字列一致用の正規化(従来の比較の上位互換)。"""
    text = unicodedata.normalize('NFKC', str(value)).strip().upper()
    return re.sub(r'\s+', ' ', text)


def _is_safe(raw: str) -> bool:
    """parse_expr(内部でeval)に渡してよい入力かを判定する。"""
    if not raw or len(raw) > MAX_LENGTH:
        return False
    if any(ch in _FORBIDDEN_CHARS for ch in raw):
        return False
    if '__' in raw or _ATTRIBUTE_ACCESS.search(raw):
        return False
    if _FORBIDDEN_WORDS.search(raw):
        return False
    return True


def _preprocess(raw: str) -> str:
    """表記ゆれを統一して、sympyに渡せるASCII式に変換する。"""
    text = raw
    # 上付き文字: x² → x^(2), x¹⁰ → x^(10), x⁻¹ → x^(-1)
    text = _SUPERSCRIPT_RUN.sub(lambda m: '^(' + m.group().translate(_SUPERSCRIPT_MAP) + ')', text)
    text = unicodedata.normalize('NFKC', text).translate(_CHAR_MAP)
    text = text.strip()

    # 1,000 のような桁区切りカンマを除去（"1,2" のような組の答えは対象外）
    if re.fullmatch(r'[+-]?\d{1,3}(,\d{3})+(\.\d+)?', text):
        text = text.replace(',', '')

    # 三角関数内の度数法 sin30° / sin(30°) → sin(30*pi/180)
    text = re.sub(
        r'(sin|cos|tan)\s*\(?\s*([0-9]+(?:\.[0-9]+)?)\s*°\s*\)?',
        r'\1(\2*pi/180)',
        text,
    )
    if '°' in text:
        if re.search(r'sin|cos|tan', text):
            raise _Unparseable('三角関数内の複雑な度数表記')
        text = text.replace('°', '')

    # 平方根: 2√3 → 2*sqrt(3), √(x+1) → sqrt(x+1)
    text = re.sub(r'([0-9A-Za-z)])\s*√', r'\1*√', text)
    text = re.sub(r'√\s*\(', 'sqrt(', text)
    text = re.sub(r'√\s*([0-9]+(?:\.[0-9]+)?|[A-Za-z])', r'sqrt(\1)', text)

    # sin30 のような関数名と数字の連結を分離
    text = re.sub(r'\b(sin|cos|tan|log|ln|sqrt)(?=[0-9])', r'\1 ', text)

    if not _ALLOWED_AFTER_PREPROCESS.match(text):
        raise _Unparseable('式として解釈できない文字を含む')
    return text


def _looks_mathy(text: str) -> bool:
    """数字・演算子・関係記号のいずれも含まない(選択肢記号や単語など)入力は数式として扱わない。"""
    return bool(re.search(r'[0-9+\-*/^()<>=]', text))


# ---------------------------------------------------------------- 解釈 (層1)

def _check_exponents(expr: sp.Basic) -> None:
    """9^9^9 のような巨大な冪の評価で処理が止まらないよう、評価前に指数を検査する。"""
    for node in sp.preorder_traversal(expr):
        if isinstance(node, sp.Pow) and node.exp.is_number:
            try:
                magnitude = abs(float(node.exp.evalf()))
            except Exception:
                raise _Unparseable('指数を評価できない')
            if magnitude > MAX_EXPONENT:
                raise _Unparseable('指数が大きすぎる')


def _to_expr(part: str) -> sp.Basic:
    part = part.strip()
    if not part:
        raise _Unparseable('空の式')
    try:
        with warnings.catch_warnings():  # 解釈できない式で出る sympy の非推奨警告は表示しない（解釈失敗として扱う）
            warnings.simplefilter('ignore')
            return _to_expr_inner(part)
    except _Unparseable:
        raise
    except (Exception, RecursionError) as exc:
        raise _Unparseable(f'解釈失敗: {exc.__class__.__name__}')


def _to_expr_inner(part: str) -> sp.Basic:
    try:
        unevaluated = parse_expr(
            part, local_dict={}, global_dict=dict(_ALLOWED_NAMES),
            transformations=_TRANSFORMATIONS, evaluate=False,
        )
        unevaluated = sp.Tuple(*unevaluated) if isinstance(unevaluated, tuple) else unevaluated
        if not isinstance(unevaluated, sp.Basic):
            raise _Unparseable('式ではない')
        _check_exponents(unevaluated)
        result = parse_expr(
            part, local_dict={}, global_dict=dict(_ALLOWED_NAMES),
            transformations=_TRANSFORMATIONS,
        )
    except _Unparseable:
        raise
    except (Exception, RecursionError) as exc:
        raise _Unparseable(f'解釈失敗: {exc.__class__.__name__}')
    if isinstance(result, tuple):
        result = sp.Tuple(*result)
    if not isinstance(result, sp.Basic):
        raise _Unparseable('式ではない')
    return result


def _make_relation(op: str, left: sp.Basic, right: sp.Basic):
    if op in ('=', '=='):
        return sp.Eq(left, right)
    if op == '!=':
        return sp.Ne(left, right)
    if op == '<':
        return sp.Lt(left, right)
    if op == '<=':
        return sp.Le(left, right)
    if op == '>':
        return sp.Gt(left, right)
    return sp.Ge(left, right)


def _parse_item(text: str) -> Tuple[str, Any]:
    """1つの答えを ('expr', 式) または ('rel', [関係式...]) に解釈する。"""
    pieces = _RELATION_SPLIT.split(text)
    if len(pieces) == 1:
        return 'expr', _to_expr(pieces[0])
    operands = [_to_expr(p) for p in pieces[0::2]]
    operators = pieces[1::2]
    if any(isinstance(o, sp.Tuple) for o in operands):
        raise _Unparseable('組を含む関係式')
    relations = [
        _make_relation(op, operands[i], operands[i + 1])
        for i, op in enumerate(operators)
    ]
    return 'rel', relations


def _split_top_level(text: str) -> List[str]:
    """括弧の外にあるカンマで区切る。"""
    items: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            items.append(''.join(buf))
            buf = []
        else:
            buf.append(ch)
    items.append(''.join(buf))
    return [i.strip() for i in items if i.strip()]


# ---------------------------------------------------------------- 判定 (層3)

def _decimal_places(expected_text: str) -> Optional[int]:
    """期待値が小数(「x=23.6」のような単純な等式を含む)なら小数点以下の桁数を返す。"""
    value_text = re.sub(r'^[A-Za-z]\w*\s*=\s*', '', expected_text.strip())
    match = _DECIMAL_LITERAL.match(value_text)
    return len(match.group(1)) if match else None


def _numeric_value(expr: sp.Basic) -> Optional[complex]:
    try:
        return complex(sp.N(expr))
    except Exception:
        return None


def _compare_exprs(exp: sp.Basic, act: sp.Basic, places: Optional[int],
                   rel_tol: float = 0.0) -> Tuple[bool, str, str]:
    if isinstance(exp, sp.Tuple) or isinstance(act, sp.Tuple):
        if not (isinstance(exp, sp.Tuple) and isinstance(act, sp.Tuple)) or len(exp) != len(act):
            return False, 'symbolic_mismatch', '組の要素数が異なります'
        for e, a in zip(exp, act):
            ok, _, _ = _compare_exprs(e, a, None)
            if not ok:
                return False, 'symbolic_mismatch', '組の要素が一致しません'
        return True, 'symbolic_equal', '組の各要素が数式として同値です'

    diff = exp - act
    if diff == 0 or diff.is_zero:
        return True, 'symbolic_equal', '数式として同値です'
    free = exp.free_symbols | act.free_symbols
    try:
        if sp.simplify(diff) == 0:
            return True, 'symbolic_equal', '数式として同値です(simplify)'
    except Exception:
        pass
    if free:
        return False, 'symbolic_mismatch', '数式が同値ではありません'

    # 数値のみ: 期待値が小数なら指定桁での四捨五入を許容、それ以外は厳密に近い許容誤差
    exp_val, act_val = _numeric_value(exp), _numeric_value(act)
    if exp_val is None or act_val is None:
        return False, 'numeric_mismatch', '数値として評価できません'
    tolerance = 0.5 * 10 ** (-places) + 1e-12 if places is not None else 1e-9 * max(1.0, abs(exp_val))
    tolerance = max(tolerance, rel_tol * abs(exp_val))
    if abs(exp_val - act_val) <= tolerance:
        return True, 'numeric_equal', f'数値が許容誤差{tolerance:g}以内で一致します'
    return False, 'numeric_mismatch', '数値が一致しません'


def _solution_set(relations: List[Any], var: sp.Symbol):
    result = sp.S.Reals
    for rel in relations:
        result = result.intersect(sp.solveset(rel, var, domain=sp.S.Reals))
    return result


def _canonical(rel: Any) -> Tuple[str, sp.Basic]:
    """関係式を「式 op 0」(op は >, >=, =, !=) に揃える。"""
    if isinstance(rel, sp.Eq):
        return '=', rel.lhs - rel.rhs
    if isinstance(rel, sp.Ne):
        return '!=', rel.lhs - rel.rhs
    if isinstance(rel, sp.StrictGreaterThan):
        return '>', rel.lhs - rel.rhs
    if isinstance(rel, sp.GreaterThan):
        return '>=', rel.lhs - rel.rhs
    if isinstance(rel, sp.StrictLessThan):
        return '>', rel.rhs - rel.lhs
    return '>=', rel.rhs - rel.lhs


def _compare_relations(exp_rels: List[Any], act_rels: List[Any]) -> Tuple[bool, str, str]:
    if any(isinstance(r, sp.logic.boolalg.BooleanAtom) for r in exp_rels + act_rels):
        same = exp_rels == act_rels
        return same, 'set_equal' if same else 'set_mismatch', '定数の関係式を比較しました'

    exp_free = set().union(*(r.free_symbols for r in exp_rels))
    act_free = set().union(*(r.free_symbols for r in act_rels))
    if exp_free != act_free:
        return False, 'set_mismatch', '使われている変数が異なります'

    if len(exp_free) == 1:
        var = next(iter(exp_free))
        try:
            exp_set = _solution_set(exp_rels, var)
            act_set = _solution_set(act_rels, var)
            if exp_set == act_set or exp_set.symmetric_difference(act_set) == sp.S.EmptySet:
                return True, 'set_equal', '解集合が一致します'
            return False, 'set_mismatch', '解集合が一致しません'
        except Exception:
            pass  # solveset が扱えない場合は下の標準形比較へ

    # 複数変数など: 各関係式を標準形にして、定数倍で一致するかを調べる
    if len(exp_rels) != len(act_rels):
        return False, 'set_mismatch', '関係式の数が異なります'
    remaining = [_canonical(r) for r in act_rels]
    for rel in exp_rels:
        op, diff = _canonical(rel)
        for idx, (a_op, a_diff) in enumerate(remaining):
            if a_op != op:
                continue
            try:
                ratio = sp.simplify(diff / a_diff)
            except Exception:
                continue
            if ratio.free_symbols or ratio == 0 or not ratio.is_real:
                continue
            if op in ('>', '>=') and ratio < 0:
                continue
            remaining.pop(idx)
            break
        else:
            return False, 'set_mismatch', '関係式が同値ではありません'
    return True, 'set_equal', '関係式が同値です(標準形)'


def _unwrap_simple_equation(kind: str, obj: Any) -> Tuple[str, Any]:
    """「x=2」のような単純な等式を値 2 として扱う(相手が値だけのとき用)。"""
    if kind == 'rel' and len(obj) == 1 and isinstance(obj[0], sp.Eq):
        lhs, rhs = obj[0].lhs, obj[0].rhs
        if isinstance(lhs, sp.Symbol) and not rhs.free_symbols:
            return 'expr', rhs
        if isinstance(rhs, sp.Symbol) and not lhs.free_symbols:
            return 'expr', lhs
    return kind, obj


def _compare_items(exp_item: Tuple[str, Any], act_item: Tuple[str, Any],
                   places: Optional[int]) -> Tuple[bool, str, str]:
    exp_kind, exp_obj = exp_item
    act_kind, act_obj = act_item
    if exp_kind != act_kind:
        exp_kind, exp_obj = _unwrap_simple_equation(exp_kind, exp_obj) if act_kind == 'expr' else (exp_kind, exp_obj)
        act_kind, act_obj = _unwrap_simple_equation(act_kind, act_obj) if exp_kind == 'expr' else (act_kind, act_obj)
    if exp_kind != act_kind:
        return False, 'type_mismatch', '式と関係式など、答えの形式が異なります'
    if exp_kind == 'expr':
        return _compare_exprs(exp_obj, act_obj, places)
    return _compare_relations(exp_obj, act_obj)


def _max_matching(compatible: List[List[bool]], k: int) -> int:
    """二部グラフ(生徒の答え × 期待値)の最大マッチング数を増加路法で求める。"""
    owner: List[Optional[int]] = [None] * k

    def assign(j: int, seen: List[bool]) -> bool:
        for i in range(k):
            if compatible[j][i] and not seen[i]:
                seen[i] = True
                if owner[i] is None or assign(owner[i], seen):
                    owner[i] = j
                    return True
        return False

    return sum(1 for j in range(len(compatible)) if assign(j, [False] * k))


def _expand_plus_minus(items: List[str]) -> List[str]:
    """「x=±√3」を「x=+√3」「x=-√3」の2つの答えに展開する。"""
    expanded: List[str] = []
    for item in items:
        count = item.count('±')
        if count == 0:
            expanded.append(item)
        elif count == 1:
            expanded.extend([item.replace('±', '+'), item.replace('±', '-')])
        else:
            raise _Unparseable('± が複数ある')
    return expanded


def _judge_math(expected: str, actual: str) -> Optional[Dict[str, Any]]:
    """数式として判定する。解釈できない場合は None(呼び出し側で文字列一致へ)。"""
    if not (_is_safe(expected) and _is_safe(actual)):
        return None
    try:
        exp_text, act_text = _preprocess(expected), _preprocess(actual)
        if not (_looks_mathy(exp_text) and _looks_mathy(act_text)):
            return None
        exp_texts = _expand_plus_minus(_split_top_level(exp_text))
        act_texts = _expand_plus_minus(_split_top_level(act_text))
        exp_items = [_parse_item(t) for t in exp_texts]
        act_items = [_parse_item(t) for t in act_texts]
    except _Unparseable:
        return None
    if not exp_items or not act_items:
        return None

    exp_places = [_decimal_places(t) for t in exp_texts]

    if len(exp_items) == 1 and len(act_items) == 1:
        ok, method, detail = _compare_items(exp_items[0], act_items[0], exp_places[0])
        return {'is_correct': ok, 'method': method, 'detail': detail}

    # 組の答え: 順不同でペア照合（許容誤差つきの一致は推移的でないため、貪欲ではなく最大マッチングで数える）
    k, n = len(exp_items), len(act_items)
    compatible = [
        [_compare_items(exp_items[i], act_items[j], exp_places[i])[0] for i in range(k)]
        for j in range(n)
    ]
    matched = _max_matching(compatible, k)
    if matched == k and n == k:
        return {'is_correct': True, 'method': 'list_equal', 'detail': f'{k}個すべて一致しました'}
    if n < k and matched == n:
        return {'is_correct': False, 'method': 'incomplete', 'detail': f'答えが{k}個必要ですが{n}個しかありません'}
    if n > k and matched == k:
        return {'is_correct': False, 'method': 'extra_items', 'detail': f'答えが{k}個のところ{n}個書かれています'}
    return {'is_correct': False, 'method': 'list_mismatch', 'detail': f'{k}個中{matched}個のみ一致しました'}


def _string_fallback(expected: str, actual: str) -> Dict[str, Any]:
    if _normalize_text(expected) == _normalize_text(actual):
        return {'is_correct': True, 'method': 'string_exact', 'detail': '文字列として一致します'}
    return {'is_correct': False, 'method': 'string_mismatch', 'detail': '文字列として一致しません'}


# ---------------------------------------------------------------- 結果の返却 (層4)

def _judge_answer_direct(expected: Any, actual: Any) -> Dict[str, Any]:
    """期待値と生徒の解答を比較する。

    戻り値: {'is_correct': bool, 'method': str, 'detail': str}
    method: symbolic_equal / numeric_equal / set_equal / list_equal / string_exact (正解)
            *_mismatch / incomplete / extra_items / type_mismatch / empty (不正解)
    """
    if expected is None or actual is None or not str(expected).strip() or not str(actual).strip():
        return {'is_correct': False, 'method': 'empty', 'detail': '期待値または解答が空です'}

    expected_text, actual_text = str(expected), str(actual)
    verdict = _judge_math(_delatex(expected_text), _delatex(actual_text))
    if verdict is None:
        return _string_fallback(expected_text, actual_text)
    if not verdict['is_correct'] and _normalize_text(expected_text) == _normalize_text(actual_text):
        # 大文字小文字だけの差(x と X など)は従来の採点どおり正解とする
        return {'is_correct': True, 'method': 'string_exact', 'detail': '文字列として一致します'}
    return verdict


# ---------------------------------------------------------------- 解説の検算 (Chain 4)

_IDENTIFIER = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')


def _format_value(expr: sp.Basic) -> str:
    if expr.has(sp.Float):
        return _restore_primes(str(sp.N(expr, 6)))
    return _restore_primes(str(sp.simplify(expr)))


_DECIMAL_IN_TEXT = re.compile(r'\d*\.\d+')


def _approximation_tolerance(*texts: str) -> float:
    """式に近似値（三角比の表の 0.7002 など）が含まれるとき，その有効数字に応じた相対誤差を返す。

    表の値は丸めてあるため，20 × 0.7002 = 14.004 を厳密な 20·tan35° = 14.0042… と比べても
    誤りとしないようにする。近似値を含まない式では 0（厳密に比較）。
    """
    digits = []
    for text in texts:
        for literal in _DECIMAL_IN_TEXT.findall(text):
            significant = literal.replace('.', '').lstrip('0')
            if significant:
                digits.append(len(significant))
    return 0.5 * 10 ** (1 - min(digits)) if digits else 0.0


def is_math_expression(text: Any) -> bool:
    """sympy で値の式として安全に解釈できるか。"""
    raw = _delatex(str(text).strip())
    if not _is_safe(raw):
        return False
    try:
        return isinstance(_to_expr(_preprocess(raw)), sp.Expr)
    except _Unparseable:
        return False


def _check_claim_direct(lhs: Any, rhs: Any, definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """解説中の計算の主張「lhs = rhs」を検算する（卒業論文 3.5節，Chain 4）。

    definitions: それまでに解説中で定義された文字 {名前: 式}（例: {'D': '64-12*m'}）。代入してから比較する。
    戻り値 status:
      ok           : 成り立つ（rhs が小数なら，その桁数での四捨五入の範囲で一致）
      ng           : 成り立たない（true_value に左辺の正しい値）
      definition   : 左辺が新しい文字の定義（D = b^2-4ac など）。呼び出し側で definitions に加える
      unverifiable : 式として解釈できない，または両辺の文字が異なり恒等式として判定できない
    """
    lhs_text, rhs_text = _delatex(str(lhs).strip()), _delatex(str(rhs).strip())
    result: Dict[str, Any] = {'status': 'unverifiable', 'true_value': None, 'detail': ''}
    if not (_is_safe(lhs_text) and _is_safe(rhs_text)):
        result['detail'] = '安全に解釈できない文字を含むため検算しませんでした'
        return result
    try:
        lhs_pre, rhs_pre = _preprocess(lhs_text), _preprocess(rhs_text)
        left, right = _to_expr(lhs_pre), _to_expr(rhs_pre)
        substitutions = {}
        for name, value in (definitions or {}).items():
            name, value_text = symbol_name(name), _delatex(str(value))
            if _IDENTIFIER.match(name) and _is_safe(value_text):
                substitutions[sp.Symbol(name)] = _to_expr(_preprocess(value_text))
    except _Unparseable as exc:
        result['detail'] = f'式として解釈できませんでした（{exc}）'
        return result
    if not (isinstance(left, sp.Expr) and isinstance(right, sp.Expr)):
        result['detail'] = '等式の両辺が値の式ではありません'
        return result

    if _IDENTIFIER.match(lhs_pre) and sp.Symbol(lhs_pre) not in substitutions \
            and sp.Symbol(lhs_pre) not in right.free_symbols:
        return {'status': 'definition', 'true_value': None,
                'detail': f'{_restore_primes(lhs_pre)} の定義として記録しました'}

    for _ in range(5):  # D = b^2-4ac，a = 2 … のような定義の連鎖を順に代入する
        new_left, new_right = left.subs(substitutions), right.subs(substitutions)
        if new_left == left and new_right == right:
            break
        left, right = new_left, new_right
    if left.free_symbols != right.free_symbols:
        result['detail'] = '両辺の文字が異なるため，恒等式として検算できません'
        return result

    ok, method, detail = _compare_exprs(right, left, _decimal_places(rhs_pre), _approximation_tolerance(lhs_pre, rhs_pre))
    if ok:
        return {'status': 'ok', 'true_value': None, 'detail': detail}
    try:
        true_value = _format_value(left)
    except Exception:
        true_value = None
    return {'status': 'ng', 'true_value': true_value, 'detail': detail}


# ---------------------------------------------------------------- 不等式・方程式の変形の検算 (Chain 4)

class _Unverifiable(Exception):
    pass


def _definition_substitutions(definitions: Optional[Dict[str, str]]) -> Dict[sp.Symbol, sp.Basic]:
    substitutions = {}
    for name, value in (definitions or {}).items():
        name, value_text = symbol_name(name), _delatex(str(value))
        if _IDENTIFIER.match(name) and _is_safe(value_text):
            substitutions[sp.Symbol(name)] = _to_expr(_preprocess(value_text))
    return substitutions


def _substitute(obj, substitutions):
    for _ in range(5):
        new = obj.subs(substitutions)
        if new == obj:
            break
        obj = new
    return obj


def _relation_solution(text: str, definitions: Optional[Dict[str, str]]):
    """関係式（「m=2, -6」のような組を含む）の解集合を (変数, 解集合) で返す。"""
    raw = _delatex(str(text).strip())
    if not _is_safe(raw):
        raise _Unverifiable('安全に解釈できない文字を含む')
    try:
        pre = _preprocess(raw)
        substitutions = _definition_substitutions(definitions)
        items = [_parse_item(t) for t in _expand_plus_minus(_split_top_level(pre))]
    except _Unparseable as exc:
        raise _Unverifiable(f'式として解釈できない（{exc}）')
    if not items or items[0][0] != 'rel':
        raise _Unverifiable('関係式ではない')

    groups = []
    variables = set()
    for kind, obj in items:
        if kind == 'rel':
            relations = [_substitute(r, substitutions) for r in obj]
        elif groups and len(variables) == 1:  # 「m=2, -6」の -6 は直前と同じ変数の値
            relations = [sp.Eq(next(iter(variables)), _substitute(obj, substitutions))]
        else:
            raise _Unverifiable('関係式ではない項を含む')
        for relation in relations:
            if isinstance(relation, sp.logic.boolalg.BooleanAtom):
                raise _Unverifiable('文字を含まない関係式になる')
            variables |= relation.free_symbols
        groups.append(relations)
    if len(variables) != 1:
        raise _Unverifiable('文字が1つの関係式でないため，解集合で比較できない')
    var = next(iter(variables))
    for relations in groups:
        for relation in relations:
            if any(a.has(sp.Abs) for a in relation.atoms(sp.Abs) if a.args and a.args[0].has(sp.Abs)):
                raise _Unverifiable('絶対値が入れ子になっていて，解集合を求めると時間がかかりすぎる')

    def solve_all():
        solution = sp.S.EmptySet
        for relations in groups:
            solution = solution.union(_solution_set(relations, var))
        return solution

    try:
        solution = solve_all()
    except Exception as exc:
        raise _Unverifiable(f'解集合を求められない（{exc.__class__.__name__}）')
    return var, solution


def solved_piece_variable(text: Any) -> Optional[str]:
    """「x ≦ 2-√3」「2+√3 ≦ x」「0 < x < 2」「x = 1」のように，文字1つと定数だけの関係式（解の一部）なら
    その文字の名前を返す。「x ≦ 2-√3，2+√3 ≦ x」のように並べた答えが分かれて抜き出されたとき，合わせて比べるために使う。
    """
    raw = _delatex(str(text).strip())
    if not _is_safe(raw):
        return None
    variable: Optional[str] = None
    try:
        for part in _expand_plus_minus(_split_top_level(_preprocess(raw))):
            kind, obj = _parse_item(part)
            if kind != 'rel':
                return None
            for relation in obj:
                if not isinstance(relation, sp.core.relational.Relational):
                    return None
                symbols = relation.free_symbols
                left, right = relation.lhs, relation.rhs
                lone = left if isinstance(left, sp.Symbol) and not right.free_symbols else \
                    right if isinstance(right, sp.Symbol) and not left.free_symbols else None
                if lone is None or len(symbols) != 1 or (variable and str(lone) != variable):
                    return None
                variable = str(lone)
    except Exception:  # 解釈できないものは「解の一部ではない」として扱う
        return None
    return variable


def solved_values(text: Any) -> Optional[Tuple[Optional[str], List[str]]]:
    """「x = 1-sqrt(6)」「x = 1-√6, 1+√6」のような解いた形なら (文字, [値, ...])，
    「1+sqrt(6)」のような文字を含まない値だけなら (None, [値])，それ以外は None を返す。

    複数の解を並べた書き方（x = 1-√6, 1+√6）が別々の式として抜き出されたときに，1つの解のリストに
    まとめ直すために使う（解いた形どうしを変形として比べると，正しい解答が「誤り」になるため）。
    """
    raw = _delatex(str(text).strip())
    if not _is_safe(raw):
        return None
    variable: Optional[str] = None
    values: List[str] = []
    try:
        parts = _expand_plus_minus(_split_top_level(_preprocess(raw)))
        for part in parts:
            kind, obj = _parse_item(part)
            if kind == 'rel':
                if len(obj) != 1 or not isinstance(obj[0], sp.Equality):
                    return None
                left, right = obj[0].lhs, obj[0].rhs
                if not isinstance(left, sp.Symbol) or right.free_symbols or (variable and str(left) != variable):
                    return None
                variable = str(left)
                values.append(part.split('=', 1)[1].strip())
            elif isinstance(obj, sp.Expr) and not obj.free_symbols:
                values.append(part.strip())
            else:
                return None
    except Exception:  # 解釈できないものは「解いた形ではない」として扱う
        return None
    return (variable, values) if values else None


def _check_relation_step_direct(previous: Any, current: Any, definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """不等式・方程式の変形「previous → current」が同値か（解集合が一致するか）を検算する。

    戻り値 status: ok / ng / unverifiable。ng のときは両方の解集合を返す。
    """
    try:
        prev_var, prev_set = _relation_solution(previous, definitions)
        cur_var, cur_set = _relation_solution(current, definitions)
    except _Unverifiable as exc:
        return {'status': 'unverifiable', 'detail': str(exc), 'previous_solution': None, 'current_solution': None}
    if prev_var != cur_var:
        return {'status': 'unverifiable', 'detail': '変形の前後で文字が異なります',
                'previous_solution': None, 'current_solution': None}
    same = prev_set == cur_set or prev_set.symmetric_difference(cur_set) == sp.S.EmptySet
    return {
        'status': 'ok' if same else 'ng',
        'detail': '解集合が一致します' if same else '変形の前後で解集合が一致しません',
        'previous_solution': _format_solution(prev_var, prev_set),
        'current_solution': _format_solution(cur_var, cur_set),
    }


def _check_relation_combination_direct(previous_list: List[Any], current: Any, mode: str,
                                      definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """変形して得た式が，前の式どうしの共通範囲（mode='and'）または合わせた範囲（mode='or'）と一致するか。

    「1 < m < 2 と m > 4/3 の共通範囲は 4/3 < m < 2」「(i) と (ii) を合わせて 2/3 ≦ m < 2」のような変形を確かめる。
    """
    try:
        sets = [_relation_solution(text, definitions) for text in previous_list]
        cur_var, cur_set = _relation_solution(current, definitions)
    except _Unverifiable as exc:
        return {'status': 'unverifiable', 'detail': str(exc), 'previous_solution': None, 'current_solution': None}
    if any(var != cur_var for var, _ in sets):
        return {'status': 'unverifiable', 'detail': '式の文字が異なります', 'previous_solution': None, 'current_solution': None}
    combined = sets[0][1]
    for _, solution in sets[1:]:
        combined = combined.intersect(solution) if mode == 'and' else combined.union(solution)
    same = combined == cur_set or combined.symmetric_difference(cur_set) == sp.S.EmptySet
    word = '共通範囲' if mode == 'and' else '合わせた範囲'
    return {
        'status': 'ok' if same else 'ng',
        'detail': f'前の式の{word}と解集合が一致します' if same else f'前の式の{word}と解集合が一致しません',
        'previous_solution': _format_solution(cur_var, combined),
        'current_solution': _format_solution(cur_var, cur_set),
    }


def _format_solution(var: sp.Symbol, solution) -> str:
    """解集合を「m < 49/8」「m = -6, 2」のような読みやすい形にする。"""
    if solution == sp.S.EmptySet:
        return '解なし'
    if solution == sp.S.Reals:
        return 'すべての実数'
    if isinstance(solution, sp.FiniteSet):
        return _restore_primes(f'{var} = ' + ', '.join(str(v) for v in sorted(solution, key=lambda v: float(v))))
    try:
        text = str(solution.as_relational(var))
    except Exception:
        return str(solution)
    text = re.sub(r'\(-oo < (\w+)\) & ', '', text)  # 無限大の側の条件は省く
    text = re.sub(r' & \((\w+) < oo\)', '', text)
    text = re.sub(r'\(\(([^()]*)\)\)', r'(\1)', text)
    if re.fullmatch(r'\([^()]*\)', text):
        text = text[1:-1]
    return _restore_primes(text.replace('&', 'かつ').replace('|', 'または').replace('<=', '≦').replace('>=', '≧'))


# ---------------------------------------------------------------- 入口（別プロセスで時間を区切って実行する）

def _plain(value: Any) -> Any:
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def judge_answer(expected: Any, actual: Any) -> Dict[str, Any]:
    """期待値と生徒の解答を比較する（_judge_answer_direct を時間を区切って実行する）。

    戻り値: {'is_correct': bool, 'method': str, 'detail': str}。時間がかかりすぎた式は文字列の一致で判定する。
    """
    from app.utils.sympy_isolation import TimeLimitExceeded, run_isolated
    try:
        return run_isolated('judge_answer', [_plain(expected), _plain(actual)])
    except TimeLimitExceeded:
        return _string_fallback(str(expected), str(actual))


def check_claim(lhs: Any, rhs: Any, definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """解説中の計算の主張「lhs = rhs」を検算する（_check_claim_direct を時間を区切って実行する）。"""
    from app.utils.sympy_isolation import TimeLimitExceeded, run_isolated
    try:
        return run_isolated('check_claim', [_plain(lhs), _plain(rhs),
                                            {str(k): _plain(v) for k, v in (definitions or {}).items()}])
    except TimeLimitExceeded as exc:
        return {'status': 'unverifiable', 'true_value': None,
                'detail': f'検算に時間がかかりすぎたため判定しませんでした（{exc}）'}


def check_relation_step(previous: Any, current: Any, definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """不等式・方程式の変形「previous → current」を検算する（_check_relation_step_direct を時間を区切って実行する）。"""
    from app.utils.sympy_isolation import TimeLimitExceeded, run_isolated
    try:
        return run_isolated('check_relation_step', [_plain(previous), _plain(current),
                                                    {str(k): _plain(v) for k, v in (definitions or {}).items()}])
    except TimeLimitExceeded as exc:
        return {'status': 'unverifiable', 'detail': f'解集合を求めるのに時間がかかりすぎたため判定しませんでした（{exc}）',
                'previous_solution': None, 'current_solution': None}


def check_relation_combination(previous_list: List[Any], current: Any, mode: str,
                               definitions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """前の式どうしの共通範囲・合わせた範囲との比較（_check_relation_combination_direct を時間を区切って実行する）。"""
    from app.utils.sympy_isolation import TimeLimitExceeded, run_isolated
    try:
        return run_isolated('check_relation_combination', [[_plain(p) for p in previous_list], _plain(current), mode,
                                                           {str(k): _plain(v) for k, v in (definitions or {}).items()}])
    except TimeLimitExceeded as exc:
        return {'status': 'unverifiable', 'detail': f'解集合を求めるのに時間がかかりすぎたため判定しませんでした（{exc}）',
                'previous_solution': None, 'current_solution': None}
