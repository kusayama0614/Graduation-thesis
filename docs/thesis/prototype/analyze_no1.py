"""verify_results.json から、発表用の集計値を出す。"""
import json, re
from collections import Counter, defaultdict
from statistics import mean

R = json.load(open("verify_results.json"))
r8, r1 = R["no8"], R["no1"]
out = {}

# ---------------- 三角比 No.8 ----------------
n_students8 = len({r["sid"] for r in r8})
judged = [r for r in r8 if r["result"] != "uncertain"]
out["no8_students"] = n_students8
out["no8_items_judged"] = len(judged)
out["no8_result"] = Counter(r["result"] for r in r8)

per_item = defaultdict(Counter)
for r in judged:
    per_item[r["item"]][r["result"]] += 1
out["no8_per_item_wrong"] = {k: v["wrong"] + v["blank"] + v["form_issue"] for k, v in per_item.items()}

# 誤答タイプ（書き起こし時の観察に基づく分類）
ERR8 = {
    ("T01", "P1_3"): "対辺・隣辺の取り違え（図の回転）", ("T05", "P1_3"): "対辺・隣辺の取り違え（図の回転）",
    ("T14", "P1_3"): "対辺・隣辺の取り違え（図の回転）", ("T22", "P1_3"): "対辺・隣辺の取り違え（図の回転）",
    ("T25", "P1_3"): "対辺・隣辺の取り違え（図の回転）",
    ("T03", "P2_1"): "有名角の値の混同", ("T03", "P2_2"): "有名角の値の混同",
    ("T14", "P2_1"): "有名角の値の混同", ("T14", "P2_3"): "有名角の値の混同",
    ("T02", "P3_2"): "表の列の読み違い（cos83°にsin83°を記入）",
    ("T01", "P5"): "計算ミス", ("T03", "P5"): "計算ミス",
    ("T01", "P6"): "立式の誤り（使う三角比の選択）", ("T15", "P6"): "目の高さを引いた（モデル化の誤り）",
}
out["no8_error_types"] = Counter(ERR8.values())

# 文字列一致 vs sympy
exact_items = {"P1_1", "P1_2", "P1_3", "P2_1", "P2_2", "P2_3"}
ex = [r for r in judged if r["item"] in exact_items and r["result"] == "correct"]
out["no8_exact_correct"] = len(ex)
out["no8_exact_correct_but_string_mismatch"] = sum(1 for r in ex if not r["string_match"])

# 自己採点との比較
wrong8 = [r for r in judged if r["result"] == "wrong"]
out["no8_wrong_total"] = len(wrong8)
out["no8_wrong_self_o"] = [(r["sid"], r["item"]) for r in wrong8 if r["self"] == "o"]
out["no8_wrong_corrected"] = sum(1 for r in wrong8 if r["corrected"])
out["no8_blank_corrected"] = sum(1 for r in judged if r["result"] == "blank" and r["corrected"])
out["no8_students_all_correct"] = sum(
    1 for s in {r["sid"] for r in r8}
    if all(r["result"] in ("correct", "correct_form_issue", "uncertain") for r in r8 if r["sid"] == s))

# ---------------- 判別式 No.1 ----------------
out["no1_sheets"] = len(r1)
for k in ("r29", "r30", "r31"):
    out[f"no1_{k}"] = Counter(r[k] for r in r1)

ERR1 = {
    "不等号の向き（負の数で割ったときの反転）": ["Q031", "Q037", "Q063", "Q067", "Q068"],
    "等号の扱い（> と ≧ / = の混同）": ["Q005", "Q083", "Q104", "Q136"],
    "近似値・範囲の誤解（16/3 を 5, 5.3 等に）": ["Q027", "Q050", "Q060", "Q105", "Q106"],
    "変数の取り違え（m ではなく D と記述）": ["Q017"],
    "−4(−m+3) の展開での符号ミス（m の値が誤り）": ["Q027", "Q057", "Q113", "Q133"],
    "m の値の転記ミス（−6 を 3 と記述）": ["Q066"],
    "重解 x=−b/2a の符号ミス": ["Q016", "Q048", "Q103", "Q104", "Q118", "Q122"],
    "重解の代入計算ミス": ["Q095", "Q121"],
    "重解を求めていない／片方のみ": ["Q001", "Q009", "Q026", "Q030", "Q058", "Q062", "Q098"],
    "係数の読み取りミス（判別式の立式）": ["Q022", "Q034", "Q049"],
    "実数解の個数の誤判定": ["Q029", "Q063", "Q097", "Q136", "Q137"],
}
out["no1_error_types"] = {k: len(v) for k, v in ERR1.items()}

# 練習30：正解だが模範表記（m<16/3）と文字列が一致しないもの
c30 = [r for r in r1 if r["r30"] == "correct"]
out["no1_30_correct"] = len(c30)
out["no1_30_correct_but_string_mismatch"] = sum(1 for r in c30 if not r["string30"])

# 自己採点で丸をつけた誤答
self_fp = []
for r in r1:
    for k, s in (("r29", "s29"), ("r30", "s30"), ("r31", "s31")):
        if r[s] == "o" and r[k] not in ("correct", "uncertain"):
            self_fp.append((r["sid"], k))
out["no1_wrong_self_o"] = self_fp

# 理解度（自己申告）と実際の正答
def pct(s):
    if not s:
        return None
    m_ = re.match(r"^(\d+)\s*%", s) or re.match(r"^(\d+)$", s)
    if m_:
        return int(m_.group(1))
    if s.startswith("80/100"):
        return 80
    if s.startswith("5〜6割"):
        return 55
    if s.startswith("40%"):
        return 45
    return None

def score(r):
    pts = 0
    pts += 1 if r["r29"] == "correct" else 0
    pts += 1 if r["r30"] == "correct" else 0
    pts += 1 if r["r31"] == "correct" else 0
    return pts

und = [r for r in r1 if r["und"]]
numeric = [(pct(r["und"]), score(r), r["sid"]) for r in und if pct(r["und"]) is not None
           and "0（○" not in r["und"] and "uncertain" not in (r["r29"], r["r30"], r["r31"])]
out["no1_und_written"] = len(und)
out["no1_und_blank"] = len(r1) - len(und)
out["no1_und_numeric"] = len(numeric)
out["no1_und_text"] = len(und) - len([r for r in und if pct(r["und"]) is not None])
hi = [s for p, s, _ in numeric if p >= 80]
lo = [s for p, s, _ in numeric if p < 80]
out["no1_und_hi_n"], out["no1_und_hi_mean_score"] = len(hi), round(mean(hi), 2) if hi else None
out["no1_und_lo_n"], out["no1_und_lo_mean_score"] = len(lo), round(mean(lo), 2) if lo else None
out["no1_und_hi_but_not_full"] = [(sid, p, s) for p, s, sid in numeric if p >= 80 and s < 3]
xs = [p for p, _, _ in numeric]; ys = [s for _, s, _ in numeric]
mx, my = mean(xs), mean(ys)
cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
vx = sum((a - mx) ** 2 for a in xs) ** 0.5; vy = sum((b - my) ** 2 for b in ys) ** 0.5
out["no1_und_pearson"] = round(cov / (vx * vy), 3) if vx and vy else None
out["no1_und_circle_or_zero"] = sum(1 for r in und if "○の可能性" in r["und"])

json.dump(out, open("summary.json", "w"), ensure_ascii=False, indent=1, default=str)
for k, v in out.items():
    print(k, ":", v)
