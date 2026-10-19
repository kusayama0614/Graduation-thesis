"""教員が作ったマスター解説の TeX をテンプレートとして扱う。

テンプレートの形（\\daimon・\\shomon・kotae・\\kangae・\\tejun・chui）と図はそのまま残し，各「手順」の本文だけを
生徒ごとに個別化する。そのために TeX を次のように分ける。

- 大問（\\daimon{番号}{題}）ごとに，小問（\\shomon{(1) ...}）の区切りで分け，問題番号「1(1)」「2」と対応させる
  （小問のない大問はそのまま1問。大問の冒頭の共通部分は最初の小問に含める）
- 小問の中を「固定の部分」と「手順」（\\tejun{題} から次の \\tejun か chui 環境の前まで）の並びに分ける
- 手順の本文の中の図・表（center 環境と tikzpicture）は ⟦図1⟧ のような印に置き換え，LLM に渡さない
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple


class TemplateError(ValueError):
    pass


FIGURE_MARK = '⟦図{}⟧'
_FIGURE_MARK = re.compile(r'⟦図(\d+)⟧')

# テンプレートに無い「復習」「類題」の見た目（教員がプリアンブルで定義していればそちらが優先される）
EXTRA_MACROS = r"""
% ---- 復習・類題の見た目（テンプレートで定義されていなければこの見た目にする） ----
\providecommand{\fukushu}[1]{\par\vspace{1pt}\noindent\tikz[baseline=(t.base)]\node[draw,dashed,inner sep=1.2pt,font=\scriptsize\sffamily](t){復習};\ #1\par}
\providecommand{\ruidai}[1]{\par\vspace{1pt}\noindent\tikz[baseline=(t.base)]\node[draw,inner sep=1.2pt,font=\scriptsize\sffamily](t){類題};\ #1\par}
"""


def _group(text: str, start: int) -> Tuple[str, int]:
    """text[start] が '{' のとき，対応する '}' までの中身と，その次の位置を返す。"""
    if start >= len(text) or text[start] != '{':
        raise TemplateError(f'{start} 文字目に {{ がありません')
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '\\':
            i += 2
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    raise TemplateError('{ と } の対応がとれません')


def _commands(text: str, name: str) -> List[int]:
    """\\name{ の位置（コメント行は除く）。"""
    positions = []
    for match in re.finditer(r'\\' + name + r'\s*\{', text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        if '%' in text[line_start:match.start()].replace('\\%', ''):
            continue
        positions.append(match.start())
    return positions


def normalize_number(text: str) -> str:
    """「１（２）」「1 (2)」などを「1(2)」にそろえる。"""
    text = unicodedata.normalize('NFKC', text or '')
    return re.sub(r'\s+', '', text)


def _replace_figures(body: str, figures: Dict[str, str]) -> str:
    """center 環境と（その外の）tikzpicture を ⟦図k⟧ に置き換える。figures に元の TeX を入れる。"""
    def repl(match):
        key = str(len(figures) + 1)
        figures[key] = match.group(0)
        return FIGURE_MARK.format(key)

    body = re.sub(r'\\begin\{center\}.*?\\end\{center\}', repl, body, flags=re.S)
    return re.sub(r'\\begin\{tikzpicture\}.*?\\end\{tikzpicture\}', repl, body, flags=re.S)


def restore_figures(body: str, figures: Dict[str, str]) -> str:
    """⟦図k⟧ を元の図に戻す。LLM が印を落とした図は本文の最後に置く（図は必ず残す）。"""
    used = set()

    def repl(match):
        key = match.group(1)
        if key in figures:
            used.add(key)
            return figures[key]
        return ''

    body = _FIGURE_MARK.sub(repl, body)
    missing = [figures[k] for k in figures if k not in used]
    return body.rstrip() + ('\n' + '\n'.join(missing) if missing else '')


def _last_math(body: str) -> str:
    """手順の本文の最後の式（ステップの結果の目安に使う）。"""
    inline = re.findall(r'\$([^$]+)\$', body)
    pick = None
    last_pos = -1
    for pattern in (r'\\\[(.*?)\\\]', r'\\begin\{(?:align|gather)\*?\}(.*?)\\end\{(?:align|gather)\*?\}'):
        for match in re.finditer(pattern, body, flags=re.S):
            if match.start() > last_pos:
                last_pos, pick = match.start(), match.group(1)
    if pick is not None:
        if '\\\\' in pick:
            pick = [line for line in pick.split('\\\\') if line.strip()][-1]
        return re.sub(r'\s+', ' ', pick.replace('&', '')).strip()
    return inline[-1].strip() if inline else ''


def _split_section(tex: str) -> Dict[str, Any]:
    """小問の TeX を，固定の部分と手順の並びに分ける。"""
    segments: List[Dict[str, Any]] = []
    figures: Dict[str, str] = {}
    tejun = _commands(tex, 'tejun')
    if not tejun:
        return {'segments': [{'type': 'fixed', 'tex': tex}], 'figures': figures}
    segments.append({'type': 'fixed', 'tex': tex[:tejun[0]]})
    for index, start in enumerate(tejun):
        brace = tex.index('{', start)
        title, after = _group(tex, brace)
        end = tejun[index + 1] if index + 1 < len(tejun) else len(tex)
        chunk = tex[after:end]
        chui = re.search(r'\\begin\{chui\}', chunk)
        body, rest = (chunk[:chui.start()], chunk[chui.start():]) if chui else (chunk, '')
        spaces = re.match(r'(\s*)(.*?)(\s*)$', body, flags=re.S)
        segments.append({'type': 'step', 'title': title.strip(), 'lead': spaces.group(1), 'trail': spaces.group(3),
                         'body': _replace_figures(spaces.group(2), figures)})
        if rest:
            segments.append({'type': 'fixed', 'tex': rest})
    return {'segments': segments, 'figures': figures}


def parse(tex: str) -> Dict[str, Any]:
    """テンプレート全体を，プリアンブル・表題・大問ごとの小問に分ける。"""
    if '\\begin{document}' not in tex or '\\end{document}' not in tex:
        raise TemplateError('\\begin{document} と \\end{document} がある TeX ファイルを選んでください')
    head, rest = tex.split('\\begin{document}', 1)
    body = rest.rsplit('\\end{document}', 1)[0]
    starts = _commands(body, 'daimon')
    if not starts:
        raise TemplateError('\\daimon{番号}{題} で始まる大問が見つかりません')
    title_block = body[:starts[0]]
    # 大問の前の区切りのコメント行は表題の側に残さない
    title_block = re.sub(r'(\n%[^\n]*)+\s*$', '\n', title_block)
    daimon = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        chunk = re.sub(r'(\n%[^\n]*)+\s*$', '\n', body[start:end])
        number, after = _group(chunk, chunk.index('{'))
        title, after = _group(chunk, after)
        content = chunk[after:]
        shomon = _commands(content, 'shomon')
        subs = []
        if not shomon:
            subs.append({'number': normalize_number(number), 'label': '', 'tex': content})
        else:
            for k, s in enumerate(shomon):
                e = shomon[k + 1] if k + 1 < len(shomon) else len(content)
                label, _ = _group(content, content.index('{', s))
                label_number = re.match(r'\s*[\(（]\s*([0-9０-９]+)\s*[\)）]', label)
                sub_number = normalize_number(number) + (f'({normalize_number(label_number.group(1))})'
                                                         if label_number else f'-{k + 1}')
                sub_tex = (content[:s] if k == 0 else '') + content[s:e]
                subs.append({'number': sub_number, 'label': label.strip(), 'tex': sub_tex})
        daimon.append({'number': normalize_number(number), 'title': title.strip(), 'subs': subs})
    return {'preamble': head, 'title_block': title_block, 'daimon': daimon}


def sections(parsed: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """{問題番号: 小問のテンプレート（segments・figures・tex）}"""
    out = {}
    for d in parsed['daimon']:
        for sub in d['subs']:
            section = _split_section(sub['tex'])
            section['tex'] = sub['tex']
            section['daimon'] = d['number']
            out[sub['number']] = section
    return out


def template_steps(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    """テンプレートの手順を解答ステップにする（前提知識ノードは後で付ける）。"""
    steps = []
    for segment in section['segments']:
        if segment['type'] != 'step':
            continue
        plain = _FIGURE_MARK.sub('（図）', segment['body'])
        steps.append({'id': len(steps) + 1, 'title': segment['title'][:200], 'description': plain[:2000],
                      'result': _last_math(segment['body'])[:500], 'nodes': []})
    return steps


def step_bodies(section: Dict[str, Any]) -> List[str]:
    return [s['body'] for s in section['segments'] if s['type'] == 'step']


def assemble(section: Dict[str, Any], bodies: List[str]) -> str:
    """固定の部分はそのまま，手順の本文だけを差し替えて小問の TeX を組み立てる（Chain 3）。"""
    parts = []
    bodies = list(bodies)
    for segment in section['segments']:
        if segment['type'] == 'fixed':
            parts.append(segment['tex'])
        else:
            own = {k: section['figures'][k] for k in _FIGURE_MARK.findall(segment['body']) if k in section['figures']}
            body = restore_figures(bodies.pop(0) if bodies else segment['body'], own)
            lead, trail = segment.get('lead') or '\n', segment.get('trail') or '\n'
            parts.append(f'\\tejun{{{segment["title"]}}}{lead}{body.strip()}{trail}')
    return ''.join(parts)


def check_latex(body: str) -> List[str]:
    """LLM が書いた手順の本文の TeX が壊れていないか（括弧・環境・数式の閉じ忘れ）を調べる。問題点の一覧を返す。"""
    problems = []
    text = re.sub(r'(?<!\\)%[^\n]*', '', _FIGURE_MARK.sub('', body))  # コメントと図の印を除く
    text = text.replace('\\\\', ' ').replace('\\{', '').replace('\\}', '').replace('\\$', '')
    depth = 0
    for ch in text:
        depth += 1 if ch == '{' else -1 if ch == '}' else 0
        if depth < 0:
            break
    if depth != 0:
        problems.append('{ と } の数が合っていません')
    stack: List[str] = []
    for kind, name in re.findall(r'\\(begin|end)\{([^}]*)\}', text):
        if kind == 'begin':
            stack.append(name)
        elif not stack or stack.pop() != name:
            problems.append(f'\\end{{{name}}} に対応する \\begin{{{name}}} がありません')
            break
    if stack:
        problems.append(f'\\begin{{{stack[-1]}}} が閉じていません')
    if text.count('\\[') != text.count('\\]'):
        problems.append('\\[ と \\] の数が合っていません')
    if len(re.findall(r'\$', text.replace('$$', ''))) % 2:
        problems.append('$ の数が奇数です（数式が閉じていません）')
    return problems


def fingerprint(section: Optional[Dict[str, Any]]) -> Optional[str]:
    if not section:
        return None
    return hashlib.sha1(json.dumps(section, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- 生徒ごとの TeX 文書

_SEAT = re.compile(r'(\\underline\{\\hspace\{([^}]*)\}\})組(\\underline\{\\hspace\{([^}]*)\}\})番')


def _title_for(title_block: str, student_id: Optional[str]) -> str:
    """表題の「□組□番」の空欄に，生徒ID「学年-組-番」の組と番を書き入れる。"""
    match = re.match(r'^(\d+)-(\d+)-(\d+)$', student_id or '')
    if not match:
        return title_block
    _, class_no, seat = match.groups()

    def repl(m):
        return (f'\\underline{{\\makebox[{m.group(2)}]{{{class_no}}}}}組'
                f'\\underline{{\\makebox[{m.group(4)}]{{{seat}}}}}番')

    return _SEAT.sub(repl, title_block, count=1)


def build_document(parsed: Dict[str, Any], students: List[Tuple[Optional[str], Dict[str, str]]],
                   label: Optional[str] = None) -> str:
    """生徒ごとの解説（{問題番号: 小問の TeX}）を，テンプレートと同じ体裁の1つの TeX 文書にする。

    複数の生徒は改ページして続ける（クラス分を一度にコンパイルして印刷できる）。解説がない小問はテンプレートのまま。
    """
    pages = []
    for index, (student_id, contents) in enumerate(students):
        parts = []
        if index:
            parts.append('\n\\clearpage\\setcounter{page}{1}\n')
        if student_id:
            parts.append(f'\n% ---- 生徒 {student_id} ----\n')
        parts.append(_title_for(parsed['title_block'], student_id))
        for d in parsed['daimon']:
            parts.append(f'\n%{"=" * 70}\n\\daimon{{{d["number"]}}}{{{d["title"]}}}')
            for sub in d['subs']:
                parts.append(contents.get(sub['number']) or sub['tex'])
        pages.append(''.join(parts))
    # 表題の右の枠（\\kaisetsulabel）。個別化した解説とマスター解説を「解説A」「解説B」のように区別がつかない名前にする
    relabel = (f'\\providecommand{{\\kaisetsulabel}}{{}}\\renewcommand{{\\kaisetsulabel}}{{{label}}}\n'
               if label else '')
    return parsed['preamble'] + EXTRA_MACROS + relabel + '\\begin{document}' + ''.join(pages) + '\n\\end{document}\n'
