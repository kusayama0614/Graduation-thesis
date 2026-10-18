"""
数学的検証チェーン（sympy）を、押野先生提供の実データ（書き起こし）に適用する。

ポイント
- 「正解」は略解の文字列ではなく、問題文の条件から sympy で毎回計算し直す（略解自体の検証も兼ねる）。
- 生徒の答えは書き起こした式文字列を sympy で解釈し、数学的同値性で判定する。
- 比較用に「略解の表記との文字列一致」でも判定し、両者のズレを数える。
- 生徒の自己採点（丸付け）と sympy 判定の不一致も数える。
"""
import json
from collections import Counter, defaultdict
import sympy as sp
from transcription import NO8_ALL as NO8, NO8_DEFAULT, NO8_NODES, NO1, NO1_DEFAULT, NO1_IDS, NO9, NO9_DEFAULT, NO9_NODES

deg = sp.pi / 180
m, x = sp.symbols("m x", real=True)

# ---------------------------------------------------------------------------
# 1. 問題文から正解を計算する（略解を使わない）
# ---------------------------------------------------------------------------
def right_triangle_ratios(opp, adj, hyp):
    assert sp.simplify(opp**2 + adj**2 - hyp**2) == 0, "三平方の定理を満たさない"
    return [sp.nsimplify(opp / hyp), sp.nsimplify(adj / hyp), sp.nsimplify(opp / adj)]

def table_round(v, nd=4):
    return round(float(v), nd)

def inverse_lookup(func, value):
    """三角比の表（1°刻み・4桁）で value に最も近い角度（整数度）を返す。"""
    best = min(range(1, 90), key=lambda d: abs(table_round(func(d * deg)) - value))
    return best

TRUTH8 = {
    # 練習1：図の辺の長さから（θの対辺・隣辺・斜辺）
    "P1_1": right_triangle_ratios(sp.Integer(1), sp.Integer(2), sp.sqrt(5)),        # A=θ, C=直角
    "P1_2": right_triangle_ratios(sp.Integer(5), sp.Integer(12), sp.Integer(13)),   # A=θ, C=直角
    "P1_3": right_triangle_ratios(2 * sp.sqrt(3), sp.Integer(2), sp.Integer(4)),    # A=θ, C=直角, 図が回転
    # 練習2：有名角
    "P2_1": [sp.cos(30 * deg), sp.tan(30 * deg)],
    "P2_2": [sp.sin(45 * deg), sp.tan(45 * deg)],
    "P2_3": [sp.sin(60 * deg), sp.cos(60 * deg)],
    # 練習3：三角比の表（4桁）
    "P3_1": [table_round(sp.sin(24 * deg))],
    "P3_2": [table_round(sp.cos(83 * deg))],
    "P3_3": [table_round(sp.tan(66 * deg))],
    # 練習4：表の逆引き
    "P4_1": [inverse_lookup(sp.sin, 0.26)],
    "P4_2": [inverse_lookup(sp.cos, 0.53)],
    "P4_3": [inverse_lookup(sp.tan, 4.3)],
    # 練習5：1m未満四捨五入（表の4桁値を使う）
    "P5": [round(500 * table_round(sp.sin(20 * deg))), round(500 * table_round(sp.cos(20 * deg)))],
    # 練習6：小数第2位を四捨五入
    "P6": [round(30 * table_round(sp.tan(40 * deg)) + 1.6, 1)],
    # 練習7：tanθ=10/100 を表で逆引き
    "P7": [inverse_lookup(sp.tan, 0.1)],
}
EXACT_ITEMS = {"P1_1", "P1_2", "P1_3", "P2_1", "P2_2", "P2_3"}

# 略解に印刷されている表記（文字列一致判定の比較用）
KEY_STRINGS8 = {
    "P1_1": [["1/sqrt(5)", "sqrt(5)/5"], ["2/sqrt(5)", "2*sqrt(5)/5"], ["1/2"]],
    "P1_2": [["5/13"], ["12/13"], ["5/12"]],
    "P1_3": [["sqrt(3)/2"], ["1/2"], ["sqrt(3)"]],
    "P2_1": [["sqrt(3)/2"], ["1/sqrt(3)"]],
    "P2_2": [["1/sqrt(2)"], ["1"]],
    "P2_3": [["sqrt(3)/2"], ["1/2"]],
}

# 2次関数 No.1 の正解
def count_real_roots(a, b, c):
    D = b**2 - 4 * a * c
    return 2 if D > 0 else (1 if D == 0 else 0)

TRUTH29 = [count_real_roots(1, 3, 1), count_real_roots(2, -1, 2), count_real_roots(4, 4, 1)]
D30 = (-8)**2 - 4 * 3 * m
TRUTH30_SET = sp.solveset(D30 > 0, m, sp.S.Reals)
D31 = m**2 - 4 * 1 * (-m + 3)
TRUTH31 = sorted((int(mv), int(-mv / 2)) for mv in sp.solve(sp.Eq(D31, 0), m))

# ---------------------------------------------------------------------------
# 2. 判定関数
# ---------------------------------------------------------------------------
def sym(s):
    return sp.sympify(s, locals={"m": m, "x": x, "D": sp.Symbol("D")})

def judge_exact(student, truth):
    return all(s is not None and sp.simplify(sym(s) - t) == 0 for s, t in zip(student, truth))

def judge_numeric(student, truth):
    out = []
    for s, t in zip(student, truth):
        if s is None:
            out.append(False)
            continue
        v = sym(s)
        if v.free_symbols:
            out.append(False)
            continue
        out.append(abs(float(v) - float(t)) < 1e-9)
    return all(out)

def judge_string(student, keys):
    norm = lambda s: str(s).replace(" ", "")
    return all(s is not None and norm(s) in [norm(k) for k in ks] for s, ks in zip(student, keys))

def judge_relation(rel_str):
    try:
        rel = sym(rel_str)
    except Exception:
        return False, "parse_error"
    if sp.Symbol("D") in rel.free_symbols:
        return False, "wrong_variable"
    st = sp.solveset(rel, m, sp.S.Reals) if not isinstance(rel, sp.And) else \
        sp.Intersection(*[sp.solveset(r, m, sp.S.Reals) for r in rel.args])
    return st == TRUTH30_SET, str(st)

# ---------------------------------------------------------------------------
# 3. 三角比 No.8 の検証
# ---------------------------------------------------------------------------
def run_no8():
    rows = []
    for sid, diffs in NO8.items():
        for item, default in NO8_DEFAULT.items():
            d = diffs.get(item, {})
            if d.get("uncertain"):
                rows.append({"sid": sid, "item": item, "result": "uncertain"})
                continue
            ans = d.get("a", default) if "a" in d else default
            if ans is None or any(a is None for a in ans):
                res = "blank"
                string_ok = False
            else:
                ok = judge_exact(ans, TRUTH8[item]) if item in EXACT_ITEMS else judge_numeric(ans, TRUTH8[item])
                res = "correct" if ok else "wrong"
                string_ok = judge_string(ans, KEY_STRINGS8[item]) if item in KEY_STRINGS8 else ok
            if res == "wrong" and d.get("form_issue"):
                res = "form_issue"
            if res == "correct" and d.get("form_issue"):
                res = "correct_form_issue"
            rows.append({
                "sid": sid, "item": item, "result": res, "string_match": string_ok,
                "self": d.get("self", "o" if res in ("correct", "correct_form_issue") else None),
                "corrected": d.get("corrected", False), "nodes": NO8_NODES[item],
                "answer": ans, "note": d.get("note") or d.get("form_issue"),
            })
    return rows

# ---------------------------------------------------------------------------
# 4. 2次関数 No.1 の検証
# ---------------------------------------------------------------------------
def parse_pairs(pairs):
    if pairs is None:
        return None
    out = []
    for mv, xv in pairs:
        out.append((sp.nsimplify(sym(str(mv))), None if xv is None else sp.nsimplify(sym(str(xv)))))
    return out

def run_no1():
    rows = []
    for sid in NO1_IDS:
        d = NO1.get(sid, {})
        # 練習29
        if d.get("u29"):
            r29 = "uncertain"
        elif "a29" in d and d["a29"] is None:
            r29 = "blank"
        else:
            r29 = "correct" if d.get("a29", NO1_DEFAULT["a29"]) == TRUTH29 else "wrong"
        # 練習30
        if d.get("u30"):
            r30, detail30 = "uncertain", None
        else:
            ok, detail30 = judge_relation(d.get("a30", NO1_DEFAULT["a30"]))
            r30 = "correct" if ok else ("form_issue" if detail30 == "wrong_variable" else "wrong")
        string30 = d.get("a30", NO1_DEFAULT["a30"]).replace(" ", "") == "m<16/3"
        # 練習31
        if d.get("u31"):
            r31 = "uncertain"
        else:
            pairs = parse_pairs(d.get("a31", NO1_DEFAULT["a31"])) if "a31" in d else parse_pairs(NO1_DEFAULT["a31"])
            if pairs is None:
                r31 = "blank"
            else:
                ms = sorted(p[0] for p in pairs)
                truth_ms = sorted(sp.Integer(t[0]) for t in TRUTH31)
                if ms != truth_ms and set(ms) < set(truth_ms):
                    r31 = "incomplete"   # m の値の一部しか答えていない
                elif ms != truth_ms:
                    r31 = "wrong_m"
                elif any(p[1] is None for p in pairs):
                    r31 = "incomplete"
                elif sorted((p[0], p[1]) for p in pairs) == sorted((sp.Integer(a), sp.Integer(b)) for a, b in TRUTH31):
                    r31 = "correct"
                else:
                    r31 = "wrong_root"
        rows.append({
            "sid": sid, "r29": r29, "r30": r30, "r31": r31, "string30": string30,
            "s29": d.get("s29"), "s30": d.get("s30"), "s31": d.get("s31"),
            "c29": d.get("c29", False), "c30": d.get("c30", False), "c31": d.get("c31", False),
            "und": d.get("und"), "a30": d.get("a30", NO1_DEFAULT["a30"]), "detail30": detail30,
        })
    return rows


# ---------------------------------------------------------------------------
# 三角比 No.9（相互関係）：正解を条件から計算する
# ---------------------------------------------------------------------------
def from_sin(s):
    c = sp.sqrt(1 - s**2); return [c, s / c]
def from_cos(c):
    s = sp.sqrt(1 - c**2); return [s, s / c]
def from_tan(t):
    c = 1 / sp.sqrt(1 + t**2); return [t * c, c]
TRUTH9 = {
    "P8_1": from_sin(sp.Rational(2, 5)), "P8_2": from_cos(sp.Rational(3, 4)), "P9": from_tan(sp.Rational(1, 2)),
    "P10_1": [sp.sin(74 * deg)], "P10_2": [sp.cos(49 * deg)], "P10_3": [sp.tan(65 * deg)],
}
KEY_STRINGS9 = {"P8_1": [["sqrt(21)/5"], ["2/sqrt(21)", "2*sqrt(21)/21"]], "P8_2": [["sqrt(7)/4"], ["sqrt(7)/3"]],
                "P9": [["1/sqrt(5)", "sqrt(5)/5"], ["2/sqrt(5)", "2*sqrt(5)/5"]]}

def judge9(item, ans):
    vals = [sp.sympify(a, locals={"deg": deg}) for a in ans]
    return all(abs(sp.N(v - t, 30)) < 1e-12 for v, t in zip(vals, TRUTH9[item]))

def run_no9():
    rows = []
    for sid, diffs in NO9.items():
        for item, default in NO9_DEFAULT.items():
            d = diffs.get(item, {})
            if d.get("uncertain"):
                rows.append({"sid": sid, "item": item, "result": "uncertain"}); continue
            ans = d.get("a", default)
            ok = judge9(item, ans)
            string_ok = judge_string(ans, KEY_STRINGS9[item]) if item in KEY_STRINGS9 else ok
            rows.append({"sid": sid, "item": item, "result": "correct" if ok else "wrong", "string_match": string_ok,
                         "self": d.get("self", "o" if ok else None), "corrected": d.get("corrected", False),
                         "answer": ans, "note": d.get("note"), "nodes": NO9_NODES[item]})
    return rows

if __name__ == "__main__":
    print("== 問題文から計算した正解（略解の検証） ==")
    for k, v in TRUTH8.items():
        print(f"  {k}: {v}")
    print("  練習29:", TRUTH29, " 練習30:", TRUTH30_SET, " 練習31:", TRUTH31)
    r8, r1, r9 = run_no8(), run_no1(), run_no9()
    print("  No.9:", TRUTH9)
    json.dump({"no8": r8, "no1": r1, "no9": r9}, open("verify_results.json", "w"), ensure_ascii=False, indent=1, default=str)
    print("\n三角比 結果内訳:", Counter(r["result"] for r in r8))
    print("No.9 結果内訳:", Counter(r["result"] for r in r9))
    print("判別式 練習29:", Counter(r["r29"] for r in r1))
    print("判別式 練習30:", Counter(r["r30"] for r in r1))
    print("判別式 練習31:", Counter(r["r31"] for r in r1))
