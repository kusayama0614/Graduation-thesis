"""演習プリントの一括取り込み（クラス・出席番号ごとの振り分け）

1. 取り込み: クラスごとの PDF（または画像）をページに分け，JPEG で保存する（PyMuPDF。poppler は不要）。
2. 見出しの読み取り: 各ページの見出し「2学期 No.1【単元】」「1年 _組 _番」から，プリント番号・学年・
   クラス・出席番号を Gemini の画像読み取りで抜き出す（氏名は読み取らない）。見出しのないページは裏面とする。
3. 振り分け: 表面に続く裏面を1枚にまとめ，「プリント × 学年 × クラス × 出席番号」ごとの答案にする。
   読めなかった項目・自信の低い読み取り・ファイルのクラスとの食い違い・同じ生徒の重複は確認が必要とする。
4. 登録: 確認の済んだ答案を AnswerSheet として登録する（生徒 ID は「学年-クラス-出席番号」）。

読み取りは並列数を絞ったスレッドで行い（BULK_READ_CONCURRENCY，既定 4），途中で止まっても未読のページから
再開できる。テストでは BULK_JOBS_INLINE で同期実行し，BULK_VISION_CLIENT で読み取りを差し替える。
"""
from __future__ import annotations

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

RENDER_DPI = 150
JPEG_QUALITY = 80
SUPPORTED_EXTENSIONS = ('.pdf', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp')

HEADER_SYSTEM = (
    'あなたは高校の数学の演習プリントのスキャン画像から，ページ上部の見出しだけを読み取る係です。'
    '見出しは印刷された「2学期 No.1 【2次関数 2次方程式の実数解の個数と判別式 (P.108〜109)】」のような行と，'
    '「1年 _組 _番 氏名」の欄（下線部に学年・クラス・出席番号の数字が手書きされる）からなります。'
    '氏名は読み取らず，出力にも含めないでください。推測で埋めず，読めない数字は 0 にしてください。'
)

HEADER_USER = (
    'このページの見出しを読み取ってください。\n'
    '- 見出し（「No.」と「年 組 番」の欄）がないページ（両面プリントの裏面など）は has_header を false にする。\n'
    '- print_no: 「No.」の後の数字。title: 【】の中の単元名（ページ範囲は除く）。\n'
    '- grade・class_no・student_no: 「年」「組」「番」の前の数字（手書き）。空欄や判読できないときは 0。\n'
    '- confidence: 数字をはっきり読めたら high，自信がなければ low。\n'
    '- note: 読み取りで気になった点（数字が消されて書き直されている，など）があれば短く書く。'
)

HEADER_SCHEMA = {
    'type': 'object',
    'properties': {
        'has_header': {'type': 'boolean'},
        'print_no': {'type': 'integer'},
        'title': {'type': 'string'},
        'grade': {'type': 'integer'},
        'class_no': {'type': 'integer'},
        'student_no': {'type': 'integer'},
        'confidence': {'type': 'string', 'enum': ['high', 'medium', 'low']},
        'note': {'type': 'string'},
    },
    'required': ['has_header', 'print_no', 'title', 'grade', 'class_no', 'student_no', 'confidence', 'note'],
}

ISSUE_LABELS = {
    'missing_print': 'プリント番号が読めない',
    'missing_class': 'クラスが読めない',
    'missing_number': '出席番号が読めない',
    'low_confidence': '読み取りの自信が低い',
    'class_mismatch': 'ファイルのクラスと違う',
    'duplicate': '同じ生徒の答案が複数ある（出し直しの可能性）',
    'orphan_back': '表面が見つからない裏面',
    'unread': 'まだ読み取っていない',
    'error': '読み取りに失敗した',
}


# ---------------------------------------------------------------- ファイル名・保存先

def parse_file_name(file_name: str, today: Optional[date] = None) -> Dict[str, Any]:
    """「1(1)-No1-0916.pdf」のようなファイル名から学年・クラス・提出日を推定する（推定できなければ None）。"""
    stem = os.path.splitext(os.path.basename(file_name or ''))[0]
    result: Dict[str, Any] = {'grade': None, 'class_no': None, 'date': None}
    match = re.match(r'^\s*(\d)\s*[\(（_\-年]\s*(\d{1,2})', stem)
    if match:
        result['grade'], result['class_no'] = int(match.group(1)), int(match.group(2))
    dates = re.findall(r'(?<!\d)(\d{2})(\d{2})(?!\d)', stem)
    if dates:
        month, day = (int(v) for v in dates[-1])
        year = (today or date.today()).year
        try:
            result['date'] = date(year, month, day).isoformat()
        except ValueError:
            pass
    return result


def batch_dir(upload_root: str, batch_id: str) -> str:
    return os.path.join(upload_root, 'bulk', batch_id)


def split_into_pages(source_path: str, out_dir: str, prefix: str) -> List[str]:
    """PDF・画像をページごとの JPEG にする。保存したファイルのパスを返す。"""
    import pymupdf

    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with pymupdf.open(source_path) as doc:
        if not doc.is_pdf:  # 画像は PDF に変換してから同じ方法で描画する
            pdf_bytes = doc.convert_to_pdf()
            doc.close()
            doc = pymupdf.open('pdf', pdf_bytes)
        for index, page in enumerate(doc):
            pixmap = page.get_pixmap(dpi=RENDER_DPI)
            path = os.path.join(out_dir, f'{prefix}_p{index + 1:03d}.jpg')
            pixmap.save(path, jpg_quality=JPEG_QUALITY)
            paths.append(path)
    return paths


def pages_to_pdf(image_paths: Iterable[str], out_path: str) -> str:
    """1枚分（表面と裏面）のページ画像を1つの PDF にまとめる。"""
    import pymupdf

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with pymupdf.open() as doc:
        for path in image_paths:
            with pymupdf.open(path) as image:
                pdf_bytes = image.convert_to_pdf()
            with pymupdf.open('pdf', pdf_bytes) as image_pdf:
                doc.insert_pdf(image_pdf)
        doc.save(out_path)
    return out_path


# ---------------------------------------------------------------- 見出しの読み取り

def read_header(client, image_path: str) -> Dict[str, Any]:
    with open(image_path, 'rb') as f:
        image_bytes = f.read()
    out = client.read_image_json(system=HEADER_SYSTEM, user=HEADER_USER, image_bytes=image_bytes,
                                 mime_type='image/jpeg', schema=HEADER_SCHEMA, schema_name='print_header',
                                 thinking_level='low')
    return normalize_reading(out)


def normalize_reading(out: Dict[str, Any]) -> Dict[str, Any]:
    def number(key):
        try:
            value = int(out.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        return value if value > 0 else None

    return {
        'has_header': bool(out.get('has_header')),
        'print_no': number('print_no'),
        'title': str(out.get('title') or '').strip(),
        'grade': number('grade'),
        'class_no': number('class_no'),
        'student_no': number('student_no'),
        'confidence': out.get('confidence') if out.get('confidence') in ('high', 'medium', 'low') else 'low',
        'note': str(out.get('note') or '').strip(),
    }


def apply_reading(page, reading: Dict[str, Any]) -> None:
    page.reading = json.dumps(reading, ensure_ascii=False)
    page.error = None
    page.status = 'read'
    page.confidence = reading['confidence']
    if reading['has_header']:
        page.kind = 'front'
        page.print_no = reading['print_no']
        page.print_title = reading['title'] or None
        page.grade = reading['grade']
        page.class_no = reading['class_no']
        page.student_no = reading['student_no']
    else:
        page.kind = 'back'


_running: set = set()
_running_lock = threading.Lock()


def start_reading(app, batch_id: str) -> bool:
    """未読・失敗のページの読み取りを始める。すでに読み取り中なら何もしない（False）。"""
    with _running_lock:
        if batch_id in _running:
            return False
        _running.add(batch_id)
    if app.config.get('BULK_JOBS_INLINE'):
        _read_batch(app, batch_id)
    else:
        threading.Thread(target=_read_batch, args=(app, batch_id), daemon=True).start()
    return True


def is_reading(batch_id: str) -> bool:
    with _running_lock:
        return batch_id in _running


def _read_batch(app, batch_id: str) -> None:
    from app import db
    from app.learning.llm import get_vision_client
    from app.models import BulkImportBatch, ScannedPage

    try:
        with app.app_context():
            client = get_vision_client(required=True)
            batch = db.session.get(BulkImportBatch, batch_id)
            if batch is None:
                return
            batch.status = 'reading'
            db.session.commit()
            targets = [(p.id, p.image_path) for p in ScannedPage.query.filter(
                ScannedPage.batch_id == batch_id, ScannedPage.status.in_(('pending', 'error'))).all()]
            workers = max(1, int(app.config.get('BULK_READ_CONCURRENCY') or os.getenv('BULK_READ_CONCURRENCY', 4)))

            def work(item):
                page_id, path = item
                try:
                    return page_id, read_header(client, path), None
                except Exception as exc:  # 1ページの失敗で全体を止めない（後で再開できる）
                    return page_id, None, f'{exc.__class__.__name__}: {str(exc)[:300]}'

            # LLM の呼び出しだけを並列にし，データベースへの書き込みはこのスレッドでまとめて行う
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, item) for item in targets]
                for future in as_completed(futures):
                    page_id, reading, error = future.result()
                    page = db.session.get(ScannedPage, page_id)
                    if page is None:
                        continue
                    if reading is not None:
                        apply_reading(page, reading)
                    else:
                        page.status = 'error'
                        page.error = error
                    db.session.commit()  # 1件ごとに確定する（書き込みの鍵を持ったまま次の読み取りを待たない）
            db.session.commit()
            batch = db.session.get(BulkImportBatch, batch_id)
            if batch is not None and batch.status == 'reading':
                batch.status = 'ready'
                db.session.commit()
    except Exception as exc:
        with app.app_context():
            batch = db.session.get(BulkImportBatch, batch_id)
            if batch is not None:
                batch.status = 'ready'
                db.session.commit()
        app.logger.warning('bulk reading failed for %s: %s', batch_id, exc)
    finally:
        with _running_lock:
            _running.discard(batch_id)


# ---------------------------------------------------------------- 振り分け

def student_id_for(grade: Optional[int], class_no: Optional[int], student_no: Optional[int]) -> Optional[str]:
    if not (grade and class_no and student_no):
        return None
    return f'{grade}-{class_no}-{student_no}'


def build_groups(pages: List[Any], files: List[Dict[str, Any]]) -> Dict[str, Any]:
    """ページを答案（表面＋続く裏面）にまとめ，確認が必要な点を付ける。"""
    file_order = {f['id']: i for i, f in enumerate(files)}
    file_info = {f['id']: f for f in files}
    ordered = sorted(pages, key=lambda p: (file_order.get(p.file_id, 10 ** 6), p.page_index))
    groups: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    current: Dict[str, Optional[Dict[str, Any]]] = {}
    for page in ordered:
        if page.kind == 'excluded':
            continue
        if page.status in ('pending', 'error') and page.kind is None:
            unresolved.append({'page_id': page.id, 'issue': 'error' if page.status == 'error' else 'unread',
                               'error': page.error})
            current[page.file_id] = None
            continue
        if page.kind == 'back':
            group = current.get(page.file_id)
            if group is not None:
                group['page_ids'].append(page.id)
                group['_pages'].append(page)
            else:
                groups.append(_new_group(page, file_info, orphan=True))
            continue
        group = _new_group(page, file_info)
        groups.append(group)
        current[page.file_id] = group

    by_key: Dict[Tuple, List[Dict[str, Any]]] = {}
    for group in groups:
        if group['key'] is not None:
            by_key.setdefault(group['key'], []).append(group)
    for same in by_key.values():
        if len(same) > 1:
            for group in same:
                group['issues'].append('duplicate')

    for group in groups:
        pages_in_group = group.pop('_pages')
        group['registered'] = any(p.answer_sheet_id for p in pages_in_group)
        group['answer_sheet_id'] = next((p.answer_sheet_id for p in pages_in_group if p.answer_sheet_id), None)
        group['key'] = list(group['key']) if group['key'] is not None else None
        group['issue_labels'] = [ISSUE_LABELS[i] for i in group['issues']]
        group['ok'] = not group['issues']
    return {'groups': groups, 'unresolved': unresolved}


def _new_group(page, file_info: Dict[str, Dict[str, Any]], orphan: bool = False) -> Dict[str, Any]:
    info = file_info.get(page.file_id, {})
    grade = page.grade or info.get('grade')
    class_no = page.class_no
    issues: List[str] = []
    if orphan:
        issues.append('orphan_back')
    else:
        confirmed = page.status == 'confirmed'
        if not page.print_no:
            issues.append('missing_print')
        if not class_no:
            issues.append('missing_class')
        if not page.student_no:
            issues.append('missing_number')
        if not confirmed and page.confidence == 'low':
            issues.append('low_confidence')
        if not confirmed and class_no and info.get('class_no') and class_no != info.get('class_no'):
            issues.append('class_mismatch')
    key = None
    if not orphan and page.print_no and grade and class_no and page.student_no:
        key = (page.print_no, grade, class_no, page.student_no)
    return {
        'front_page_id': page.id,
        'page_ids': [page.id],
        '_pages': [page],
        'file_id': page.file_id,
        'file_name': page.file_name,
        'print_no': None if orphan else page.print_no,
        'print_title': None if orphan else page.print_title,
        'grade': None if orphan else grade,
        'class_no': None if orphan else class_no,
        'student_no': None if orphan else page.student_no,
        'student_id': None if orphan else student_id_for(grade, class_no, page.student_no),
        'key': key,
        'issues': issues,
    }


def summarize(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """プリント番号 × クラスごとの答案の数（確認が必要なものを含む）。"""
    table: Dict[Tuple, Dict[str, Any]] = {}
    for group in groups:
        if not group['print_no']:
            continue
        key = (group['print_no'], group['grade'] or 0, group['class_no'] or 0)
        row = table.setdefault(key, {'print_no': key[0], 'grade': group['grade'], 'class_no': group['class_no'],
                                     'title': group['print_title'], 'sheets': 0, 'needs_review': 0,
                                     'registered': 0})
        row['sheets'] += 1
        row['needs_review'] += 0 if group['ok'] else 1
        row['registered'] += 1 if group['registered'] else 0
        row['title'] = row['title'] or group['print_title']
    return [table[k] for k in sorted(table)]


def parse_iso_date(value: Any) -> Optional[date]:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- 答えの書き起こしと採点

def sheet_pages(batch_id: str) -> Dict[str, Dict[str, Any]]:
    """登録済みの答案ごとに，ページ画像とプリント番号をまとめる。{answer_sheet_id: {print_no, page_paths, page_ids}}"""
    from app.models import ScannedPage

    sheets: Dict[str, Dict[str, Any]] = {}
    rows = (ScannedPage.query.filter(ScannedPage.batch_id == batch_id, ScannedPage.answer_sheet_id.isnot(None))
            .order_by(ScannedPage.file_id, ScannedPage.page_index).all())
    for page in rows:
        entry = sheets.setdefault(page.answer_sheet_id, {'print_no': None, 'page_paths': [], 'page_ids': []})
        if page.kind == 'front' and page.print_no:
            entry['print_no'] = page.print_no
        entry['page_paths'].append(page.image_path)
        entry['page_ids'].append(page.id)
    return sheets


def save_transcription(row, sheet, items: List[Dict[str, Any]], status: str) -> None:
    """書き起こしの結果を保存し，答案の採点結果（question_results）に書き込む。"""
    from app.learning import judgments as J
    from app.utils import transcription as T

    row.items = json.dumps(items, ensure_ascii=False)
    row.status = status
    row.error = None
    if sheet is None:
        return
    graded = [i for i in items if i['judgment'] not in J.EXCLUDED_JUDGMENTS]
    correct = sum(1 for i in graded if i['judgment'] in J.CORRECT_JUDGMENTS)
    sheet.question_results = json.dumps(T.question_results(items), ensure_ascii=False)
    sheet.ocr_text = T.ocr_text_of(items)
    sheet.correct_count = correct
    sheet.total_questions = len(graded)
    sheet.score = round(100 * correct / len(graded), 1) if graded else None
    sheet.processing_message = '答えを書き起こして採点しました' + ('（確認済み）' if status == 'confirmed' else '')
    answer_key = json.loads(sheet.answer_key) if sheet.answer_key else {}
    if not (answer_key.get('answers') if isinstance(answer_key, dict) else None):
        builtin = T.answer_key_for(row.print_no)
        if builtin:
            if isinstance(answer_key, dict) and answer_key.get('unit'):
                builtin['unit'] = answer_key['unit']
            sheet.answer_key = json.dumps(builtin, ensure_ascii=False)


def start_transcribing(app, batch_id: str, print_nos: Optional[List[int]] = None, redo: bool = False) -> int:
    """登録済みの答案の書き起こしを始める。対象の枚数を返す（読み取り中なら -1）。"""
    from app import db
    from app.models import AnswerSheet, SheetTranscription
    from app.utils import transcription as T

    key = f'tx:{batch_id}'
    with _running_lock:
        if key in _running:
            return -1
        _running.add(key)
    try:
        sheets = sheet_pages(batch_id)
        targets = []
        for sheet_id, entry in sheets.items():
            if print_nos and entry['print_no'] not in print_nos:
                continue
            if not T.key_items(entry['print_no']):
                continue  # 解答キーのないプリントは書き起こさない
            row = SheetTranscription.query.filter_by(answer_sheet_id=sheet_id).first()
            if row is None:
                answer_sheet = db.session.get(AnswerSheet, sheet_id)
                row = SheetTranscription(teacher_id=answer_sheet.teacher_id if answer_sheet else '',
                                         batch_id=batch_id, answer_sheet_id=sheet_id, print_no=entry['print_no'])
                db.session.add(row)
            elif row.status in ('done', 'confirmed') and not redo:
                continue
            row.status = 'pending'
            targets.append(sheet_id)
        db.session.commit()
    except Exception:
        with _running_lock:
            _running.discard(key)
        raise
    if app.config.get('BULK_JOBS_INLINE'):
        _transcribe(app, batch_id, targets, key)
    else:
        threading.Thread(target=_transcribe, args=(app, batch_id, targets, key), daemon=True).start()
    return len(targets)


def is_transcribing(batch_id: str) -> bool:
    with _running_lock:
        return f'tx:{batch_id}' in _running


def _transcribe(app, batch_id: str, targets: List[str], running_key: str) -> None:
    from app import db
    from app.learning.llm import get_vision_client
    from app.models import AnswerSheet, SheetTranscription
    from app.utils import transcription as T

    try:
        with app.app_context():
            client = get_vision_client(required=True)
            sheets = sheet_pages(batch_id)
            workers = max(1, int(app.config.get('BULK_READ_CONCURRENCY') or os.getenv('BULK_READ_CONCURRENCY', 4)))

            def work(sheet_id):
                entry = sheets.get(sheet_id) or {}
                key = T.key_items(entry.get('print_no'))
                try:
                    images = []
                    for path in entry.get('page_paths', []):
                        with open(path, 'rb') as f:
                            images.append((f.read(), 'image/jpeg'))
                    out = client.read_images_json(system=T.TRANSCRIBE_SYSTEM, user=T.transcribe_prompt(key),
                                                  images=images, schema=T.TRANSCRIBE_SCHEMA,
                                                  schema_name='answer_transcription')
                    return sheet_id, out, T.build_items(key, out.get('answers') or []), None
                except Exception as exc:  # 1枚の失敗で全体を止めない（後で再開できる）
                    return sheet_id, None, None, f'{exc.__class__.__name__}: {str(exc)[:300]}'

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, sheet_id) for sheet_id in targets]
                for future in as_completed(futures):
                    sheet_id, out, items, error = future.result()
                    row = SheetTranscription.query.filter_by(answer_sheet_id=sheet_id).first()
                    if row is None:
                        continue
                    row.model_name = getattr(client, 'model_name', None)
                    if error:
                        row.status = 'error'
                        row.error = error
                    else:
                        row.raw = json.dumps(out, ensure_ascii=False)
                        save_transcription(row, db.session.get(AnswerSheet, sheet_id), items, 'done')
                    db.session.commit()  # 1件ごとに確定する（書き込みの鍵を持ったまま次の書き起こしを待たない）
            db.session.commit()
    except Exception as exc:
        app.logger.warning('transcription failed for %s: %s', batch_id, exc)
    finally:
        with _running_lock:
            _running.discard(running_key)
