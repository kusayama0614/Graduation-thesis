"""OCR ユーティリティ

優先: Google Cloud Vision を使う。未利用時は pytesseract にフォールバック。
PDF ファイルは pdf2image で画像に変換してページごとにOCRを行う。

注意: pdf2image を利用するには system に poppler が必要。
"""
from __future__ import annotations

import os
import io
import shutil
from typing import List, Tuple


def _try_import_vision_client():
    try:
        from google.cloud import vision
        credentials_json = os.getenv('FIREBASE_CREDENTIALS_JSON', '').strip()
        credentials_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', '').strip()

        if credentials_json:
            import json
            from google.oauth2 import service_account
            info = json.loads(credentials_json)
            credentials = service_account.Credentials.from_service_account_info(info)
            return vision.ImageAnnotatorClient(credentials=credentials)

        if credentials_path:
            from google.oauth2 import service_account
            resolved_path = credentials_path
            if not os.path.isabs(resolved_path):
                resolved_path = os.path.abspath(resolved_path)
            credentials = service_account.Credentials.from_service_account_file(resolved_path)
            return vision.ImageAnnotatorClient(credentials=credentials)

        return vision.ImageAnnotatorClient()
    except Exception:
        return None


def _images_from_pdf(path: str):
    try:
        from pdf2image import convert_from_path
    except Exception:
        raise RuntimeError('pdf2image is required to process PDF files (install poppler and python package)')

    images = convert_from_path(path)
    pil_images = []
    for img in images:
        pil_images.append(img)
    return pil_images


def _extract_text_from_pdf(path: str) -> List[str]:
    try:
        from pypdf import PdfReader
    except Exception:
        raise RuntimeError('pypdf is required to extract text from PDF files')

    reader = PdfReader(path)
    page_texts: List[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ''
        page_texts.append(page_text)
    return page_texts


def _ocr_with_vision(client, image_bytes: bytes) -> str:
    from google.cloud import vision
    image = vision.Image(content=image_bytes)
    response = client.document_text_detection(image=image)
    if response.error.message:
        raise RuntimeError(f'Vision API error: {response.error.message}')
    return response.full_text_annotation.text


def _ocr_with_pytesseract_pil(pil_image) -> str:
    try:
        import pytesseract
    except Exception:
        raise RuntimeError('pytesseract is not available')

    # convert to RGB
    if pil_image.mode != 'RGB':
        pil_image = pil_image.convert('RGB')
    text = pytesseract.image_to_string(pil_image, lang='jpn+eng')
    return text


def _tesseract_available() -> bool:
    return shutil.which('tesseract') is not None


def get_ocr_status() -> dict:
    """OCR機能の実行可否を返す。"""
    status = {
        'vision_configured': False,
        'vision_usable': False,
        'vision_error': None,
        'tesseract_available': _tesseract_available(),
        'pdf_text_extraction_available': False,
    }

    try:
        from pypdf import PdfReader  # noqa: F401
        status['pdf_text_extraction_available'] = True
    except Exception:
        status['pdf_text_extraction_available'] = False

    client = _try_import_vision_client()
    if client is None:
        status['vision_error'] = 'Google Vision client is not configured'
        return status

    status['vision_configured'] = True

    try:
        from google.cloud import vision
        from PIL import Image, ImageDraw

        preview_image = Image.new('RGB', (120, 40), 'white')
        draw = ImageDraw.Draw(preview_image)
        draw.text((8, 8), '1 A', fill='black')
        buffer = io.BytesIO()
        preview_image.save(buffer, format='PNG')
        image = vision.Image(content=buffer.getvalue())
        response = client.document_text_detection(image=image)
        extracted_text = getattr(response.full_text_annotation, 'text', '') or ''
        if extracted_text.strip():
            status['vision_usable'] = True
        else:
            raise RuntimeError('Vision OCR returned empty text')
    except Exception as exc:
        status['vision_usable'] = False
        status['vision_error'] = str(exc)

    return status


def process_file(path: str) -> Tuple[str, List[str]]:
    """
    ファイルをOCRしてテキストを返す。
    Returns: (full_text, per_page_texts)
    """
    client = _try_import_vision_client()
    per_page_texts: List[str] = []
    full_text_parts: List[str] = []

    ext = os.path.splitext(path)[1].lower()
    if ext in ('.pdf',):
        embedded_pages = _extract_text_from_pdf(path)
        if any(page.strip() for page in embedded_pages):
            full_text = '\n\n-----PAGE_BREAK-----\n\n'.join(embedded_pages)
            return full_text, embedded_pages

        # PDF→画像変換
        pil_images = _images_from_pdf(path)
        for img in pil_images:
            page_text = None
            if client:
                try:
                    bio = io.BytesIO()
                    img.save(bio, format='PNG')
                    page_text = _ocr_with_vision(client, bio.getvalue())
                except Exception as vision_error:
                    if _tesseract_available():
                        page_text = _ocr_with_pytesseract_pil(img)
                    else:
                        raise RuntimeError(f'Vision OCR failed: {vision_error}')
            else:
                if _tesseract_available():
                    page_text = _ocr_with_pytesseract_pil(img)
                else:
                    raise RuntimeError('OCR engine not available: Google Vision client is disabled and tesseract is not installed')

            per_page_texts.append(page_text or '')
            full_text_parts.append(page_text or '')
    else:
        # 画像ファイル
        with open(path, 'rb') as f:
            data = f.read()
        if client:
            try:
                text = _ocr_with_vision(client, data)
                per_page_texts.append(text)
                full_text_parts.append(text)
            except Exception as vision_error:
                # fallback to pytesseract
                if _tesseract_available():
                    try:
                        from PIL import Image
                        img = Image.open(io.BytesIO(data))
                        text = _ocr_with_pytesseract_pil(img)
                        per_page_texts.append(text)
                        full_text_parts.append(text)
                    except Exception as e:
                        raise RuntimeError('Failed to OCR file: ' + str(e))
                else:
                    raise RuntimeError(f'Vision OCR failed: {vision_error}')
        else:
            if _tesseract_available():
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(data))
                    text = _ocr_with_pytesseract_pil(img)
                    per_page_texts.append(text)
                    full_text_parts.append(text)
                except Exception as e:
                    raise RuntimeError('Failed to OCR file: ' + str(e))
            else:
                raise RuntimeError('OCR engine not available: Google Vision client is disabled and tesseract is not installed')

    full_text = '\n\n-----PAGE_BREAK-----\n\n'.join(full_text_parts)
    return full_text, per_page_texts
