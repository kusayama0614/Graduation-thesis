import json
from collections import Counter, defaultdict
R=json.load(open("verify_results.json")); r8=R["no8"]; r9=R["no9"]
meta={m["sid"]:m["class"] for f in ("no8_meta.json","no8b_meta.json") for m in json.load(open(f))}
out={}
sids=sorted({r["sid"] for r in r8}); out["n"]=len(sids)
out["by_class"]=Counter(meta[s] for s in sids)
out["result"]=Counter(r["result"] for r in r8)
J=[r for r in r8 if r["result"]!="uncertain"]; out["judged"]=len(J)
bad=("wrong","blank","form_issue")
out["all_correct_students"]=sum(1 for s in sids if all(r["result"] in("correct","correct_form_issue","uncertain") for r in r8 if r["sid"]==s))
per=defaultdict(Counter)
for r in J: per[r["item"]][r["result"]]+=1
out["per_item"]={k:dict(v) for k,v in per.items()}
EX={"P1_1","P1_2","P1_3","P2_1","P2_2","P2_3"}
ex=[r for r in J if r["item"] in EX and r["result"]=="correct"]
out["exact_correct"]=len(ex); out["exact_string_mismatch"]=sum(1 for r in ex if not r["string_match"])
W=[r for r in J if r["result"]=="wrong"]; out["wrong"]=len(W)
out["wrong_self_o"]=[(r["sid"],r["item"]) for r in W if r["self"]=="o"]
out["wrong_corrected"]=sum(1 for r in W if r["corrected"])
B=[r for r in J if r["result"]=="blank"]; out["blank"]=len(B); out["blank_corrected"]=sum(1 for r in B if r["corrected"])
F=[r for r in J if r["result"]=="form_issue"]; out["form_issue_self_o"]=sum(1 for r in F if r["self"]=="o")
# P1_3 swap
def is_swap(r): return r["item"]=="P1_3" and r["result"]=="wrong" and str(r["answer"][0])=="1/2"
sw=[r for r in W if is_swap(r)]
out["swap_n"]=len(sw); out["swap_by_class"]=Counter(meta[r["sid"]] for r in sw); out["swap_self_o"]=sum(1 for r in sw if r["self"]=="o")
out["P1_3_judged"]=sum(per["P1_3"].values()); out["P1_3_judged_by_class"]=Counter(meta[r["sid"]] for r in J if r["item"]=="P1_3")
# error types
def et(r):
    i=r["item"]; n=(r.get("note") or "")
    if is_swap(r): return "対辺・隣辺の取り違え（図が回転した三角形）"
    if i.startswith("P1"): return "辺の比の取り違え・計算（その他）"
    if i.startswith("P2"): return "有名角の値の混同"
    if i.startswith("P3"):
        if "丸め" in n: return "表の値を指定の桁より粗く丸めた"
        return "三角比の表の読み違い（列・行・角度）"
    if i.startswith("P4"): return "表の逆引きの誤り"
    if i=="P5": return "応用（坂）：計算・丸めの誤り"
    if i=="P6": return "応用（塔）：立式・目の高さ・計算の誤り"
    return "その他"
out["error_types"]=Counter(et(r) for r in W)
out["wrong_list"]=[(r["sid"],r["item"],r["answer"],r["self"],r["corrected"],r.get("note")) for r in W]
out["per_student_bad"]=Counter(r["sid"] for r in J if r["result"] in bad)
# No.9
out["n9"]=len({r["sid"] for r in r9}); out["r9"]=Counter(r["result"] for r in r9)
ex9=[r for r in r9 if r["result"]=="correct" and r["item"] in("P8_1","P8_2","P9")]
out["n9_exact_correct"]=len(ex9); out["n9_string_mismatch"]=sum(1 for r in ex9 if not r["string_match"])
out["n9_wrong"]=[(r["sid"],r["item"],r["answer"],r["self"],r["corrected"],r["note"]) for r in r9 if r["result"]=="wrong"]
json.dump(out,open("summary_trig.json","w"),ensure_ascii=False,indent=1,default=str)
for k,v in out.items(): print(k,":",v)
