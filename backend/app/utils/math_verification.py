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

import re
import unicodedata
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
    'e': sp.E,
}

_FORBIDDEN_CHARS = set('[]{}"\';:\\@#$&`|~?!')
_FORBIDDEN_WORDS = re.compile(
    r'\b(import|lambda|for|in|if|else|is|not|and|or|exec|eval|class|def|while|with|'
    r'yield|del|global|assert|try|raise|from|as|return|pass|None|True|False)\b'
)
_ATTRIBUTE_ACCESS = re.compile(r'\.\s*[A-Za-z_]')
_ALLOWED_AFTER_PREPROCESS = re.compile(r'^[0-9A-Za-z+\-*/^().,=<>! _]*$')
_RELATION_SPLIT = re.compile(r'(<=|>=|!=|==|=|<|>)')
_DECIMAL_LITERAL = re.compile(r'^[+-]?\d+\.(\d+)$')

_SUPERSCRIPTS = {
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4',
    '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
}
_CHAR_MAP = str.maketrans({
    '−': '-', '–': '-', '—': '-', '―': '-', '‐': '-',
    '×': '*', '⋅': '*', '÷': '/',
    '≦': '<=', '≤': '<=', '≧': '>=', '≥': '>=', '≠': '!=',
    'π': 'pi', '、': ',', '，': ',',
})


class _Unparseable(Exception):
    """sympyで安全に解釈できない入力。文字列一致にフォールバックする。"""


# ---------------------------------------------------------------- 正規化 (層2)

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
    for sup, digit in _SUPERSCRIPTS.items():
        text = text.replace(sup, '^' + digit)
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
    match = _DECIMAL_LITERAL.match(expected_text.strip())
    return len(match.group(1)) if match else None


def _numeric_value(expr: sp.Basic) -> Optional[complex]:
    try:
        return complex(sp.N(expr))
    except Exception:
        return None


def _compare_exprs(exp: sp.Basic, act: sp.Basic, places: Optional[int]) -> Tuple[bool, str, str]:
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


def _judge_math(expected: str, actual: str) -> Optional[Dict[str, Any]]:
    """数式として判定する。解釈できない場合は None(呼び出し側で文字列一致へ)。"""
    if not (_is_safe(expected) and _is_safe(actual)):
        return None
    try:
        exp_text, act_text = _preprocess(expected), _preprocess(actual)
        if not (_looks_mathy(exp_text) and _looks_mathy(act_text)):
            return None
        exp_items = [_parse_item(t) for t in _split_top_level(exp_text)]
        act_items = [_parse_item(t) for t in _split_top_level(act_text)]
    except _Unparseable:
        return None
    if not exp_items or not act_items:
        return None

    places = _decimal_places(exp_text) if len(exp_items) == 1 else None

    if len(exp_items) == 1 and len(act_items) == 1:
        ok, method, detail = _compare_items(exp_items[0], act_items[0], places)
        return {'is_correct': ok, 'method': method, 'detail': detail}

    # 組の答え: 順不同でペア照合
    unused = list(range(len(exp_items)))
    matched = 0
    for act_item in act_items:
        for idx in unused:
            ok, _, _ = _compare_items(exp_items[idx], act_item, None)
            if ok:
                unused.remove(idx)
                matched += 1
                break
    k, n = len(exp_items), len(act_items)
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

def judge_answer(expected: Any, actual: Any) -> Dict[str, Any]:
    """期待値と生徒の解答を比較する。

    戻り値: {'is_correct': bool, 'method': str, 'detail': str}
    method: symbolic_equal / numeric_equal / set_equal / list_equal / string_exact (正解)
            *_mismatch / incomplete / extra_items / type_mismatch / empty (不正解)
    """
    if expected is None or actual is None or not str(expected).strip() or not str(actual).strip():
        return {'is_correct': False, 'method': 'empty', 'detail': '期待値または解答が空です'}

    expected_text, actual_text = str(expected), str(actual)
    verdict = _judge_math(expected_text, actual_text)
    if verdict is None:
        return _string_fallback(expected_text, actual_text)
    if not verdict['is_correct'] and _normalize_text(expected_text) == _normalize_text(actual_text):
        # 大文字小文字だけの差(x と X など)は従来の採点どおり正解とする
        return {'is_correct': True, 'method': 'string_exact', 'detail': '文字列として一致します'}
    return verdict
