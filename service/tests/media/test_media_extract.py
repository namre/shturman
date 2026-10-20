"""media/extract.py: вид файла, текст и картинки из фото, PDF, docx, xlsx и текста.

Образцы собираются здесь же: Pillow, pypdfium2, openpyxl и PDF, написанный вручную.
База не нужна.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from PIL import Image

from shturman.media import extract as ex
from shturman.media.extract import BadFile, Extracted, Unsupported, extract, sniff


# --- образцы -----------------------------------------------------------------------------

def _img_bytes(img: Image.Image, fmt: str, **kw) -> bytes:
    out = io.BytesIO()
    img.save(out, format=fmt, **kw)
    return out.getvalue()


def _open_jpeg(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    img.load()
    return img


_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _pdf(pages: list[str], *, password: str | None = None) -> bytes:
    """PDF вручную: по строке ASCII на страницу шрифтом Helvetica. С паролем — стандартное
    шифрование RC4 40 бит (ревизия 2): без пароля документ не открывается."""
    objs: dict[int, bytes] = {}
    n = len(pages)
    page_ids = [4 + 2 * i for i in range(n)]
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = ("<< /Type /Pages /Count %d /Kids [%s] >>" % (
        n, " ".join(f"{p} 0 R" for p in page_ids))).encode()
    objs[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    for i, text in enumerate(pages):
        pid, cid = page_ids[i], page_ids[i] + 1
        objs[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                     f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>").encode()
        lines = [text[k:k + 60] for k in range(0, len(text), 60)] or [""]
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 800 Td"]
        for line in lines:
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({safe}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs[cid] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
    trailer_extra = b""
    file_id = hashlib.md5(b"shturman-test").digest()
    if password is not None:
        pw = (password.encode() + _PAD)[:32]
        owner = (b"owner" + _PAD)[:32]
        o_value = _rc4(hashlib.md5(owner).digest()[:5], pw)
        perms = (-4).to_bytes(4, "little", signed=True)
        key = hashlib.md5(pw + o_value + perms + file_id).digest()[:5]
        u_value = _rc4(key, _PAD)
        enc_id = max(objs) + 1
        objs[enc_id] = (b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P -4 /O <"
                        + o_value.hex().encode() + b"> /U <" + u_value.hex().encode() + b"> >>")
        trailer_extra = b" /Encrypt %d 0 R" % enc_id
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n")
    xref = out.tell()
    size = max(objs) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for num in range(1, size):
        out.write(b"%010d 00000 n \n" % offsets[num])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /ID [<%s> <%s>]%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        size, file_id.hex().encode(), file_id.hex().encode(), trailer_extra, xref))
    return out.getvalue()


def _scan_pdf(n: int) -> bytes:
    """PDF из картинок без текста — как скан."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for i in range(n):
        page = doc.new_page(595, 842)
        src = Image.new("RGB", (300, 420), (40 * i % 255, 120, 200))
        buf = io.BytesIO(_img_bytes(src, "JPEG"))
        obj = pdfium.PdfImage.new(doc)
        obj.load_jpeg(buf, inline=True)
        obj.set_matrix(pdfium.PdfMatrix().scale(595, 842))
        page.insert_obj(obj)
        page.gen_content()
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def _docx(body: str, *, extra: dict[str, bytes] | None = None, document: bytes | None = None) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"/>')
        zf.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                    'openxmlformats.org/package/2006/relationships"/>')
        if document is None:
            document = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W}><w:body>'
                        f"{body}</w:body></w:document>").encode("utf-8")
        zf.writestr("word/document.xml", document)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return out.getvalue()


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _ole(extra: bytes = b"") -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504 + extra + b"\x00" * 512


# --- вид файла ---------------------------------------------------------------------------

def test_sniff_by_signature_beats_name():
    jpeg = _img_bytes(Image.new("RGB", (4, 4)), "JPEG")
    png = _img_bytes(Image.new("RGB", (4, 4)), "PNG")
    webp = _img_bytes(Image.new("RGB", (4, 4)), "WEBP")
    gif = _img_bytes(Image.new("P", (4, 4)), "GIF")
    assert sniff(jpeg, "отчёт.pdf", "application/pdf") == "image"
    assert sniff(png, None, None) == "image"
    assert sniff(webp, None, None) == "image"
    assert sniff(gif, None, None) == "image"
    assert sniff(_pdf(["hello"]), "x.txt", "text/plain") == "pdf"
    assert sniff(_docx(_p("a")), None, None) == "docx"
    assert sniff(_xlsx({"A": [[1]]}), None, None) == "xlsx"
    assert sniff("привет".encode("utf-8-sig"), None, None) == "text"


def test_sniff_text_by_name_or_mime():
    data = "Привет, мир".encode("cp1251")
    assert sniff(data, "notes.txt", None) == "text"
    assert sniff(data, "table.CSV", None) == "text"
    assert sniff(data, "readme.md", None) == "text"
    assert sniff(data, None, "text/plain; charset=windows-1251") == "text"
    assert sniff(data, None, None) is None


def test_sniff_unsupported():
    assert sniff(_ole(), "old.doc", "application/msword") is None
    assert sniff(_ole(), "old.docx", None) is None          # .doc, переименованный в .docx
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    assert sniff(heic, "IMG_0001.HEIC", "image/heic") is None
    assert sniff(heic, "photo.jpg", "image/jpeg") is None    # сигнатура главнее имени
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + b"\x00" * 64
    assert sniff(mp4, "clip.mp4", "video/mp4") is None
    assert sniff(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32, "a.rar", None) is None
    assert sniff(bytes(range(256)) * 4, None, None) is None
    assert sniff(bytes(range(256)) * 4, "a.bin", "application/octet-stream") is None
    assert sniff(b"", "a.txt", "text/plain") is None
    pptx = io.BytesIO()
    with zipfile.ZipFile(pptx, "w") as zf:
        zf.writestr("ppt/presentation.xml", "<p/>")
    assert sniff(pptx.getvalue(), "deck.pptx", None) is None
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as zf:
        zf.writestr("a.txt", "x")
    assert sniff(plain_zip.getvalue(), "docs.zip", "application/zip") is None


def test_unsupported_raises_with_reason():
    with pytest.raises(Unsupported, match="HEIC"):
        extract(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, "a.heic", None)
    with pytest.raises(Unsupported, match=r"\.doc"):
        extract(_ole(), "old.doc", None)
    with pytest.raises(Unsupported):
        extract(bytes(range(256)), None, None)
    with pytest.raises(BadFile, match="пустой"):
        extract(b"", "a.txt", None)


# --- фото --------------------------------------------------------------------------------

def test_image_large_is_downscaled():
    big = Image.new("RGB", (4000, 3000), (10, 200, 30))
    res = extract(_img_bytes(big, "JPEG", quality=70), "photo.jpg", "image/jpeg")
    assert isinstance(res, Extracted)
    assert (res.kind, res.text, res.pages, res.truncated) == ("image", "", None, False)
    assert len(res.images) == 1
    assert _open_jpeg(res.images[0]).size == (1600, 1200)
    small = extract(_img_bytes(big, "JPEG"), None, None, image_side=400)
    assert _open_jpeg(small.images[0]).size == (400, 300)


def test_image_exif_orientation_applied():
    img = Image.new("RGB", (200, 100), (255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90° по часовой
    res = extract(_img_bytes(img, "JPEG", exif=exif.tobytes()), "p.jpg", None)
    assert _open_jpeg(res.images[0]).size == (100, 200)


def test_png_with_alpha_becomes_rgb_on_white():
    img = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    res = extract(_img_bytes(img, "PNG"), "p.png", "image/png")
    out = _open_jpeg(res.images[0])
    assert out.mode == "RGB"
    assert all(c > 240 for c in out.getpixel((25, 25)))


def test_png_16bit_gray_keeps_shades():
    img = Image.new("I;16", (20, 20), 128 * 256)
    res = extract(_img_bytes(img, "PNG"), "p.png", None)
    r, g, b = _open_jpeg(res.images[0]).getpixel((10, 10))
    assert 110 < r < 150


def test_animated_gif_first_frame():
    frames = [Image.new("RGB", (40, 40), (255, 0, 0)), Image.new("RGB", (40, 40), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    res = extract(out.getvalue(), "a.gif", "image/gif")
    r, g, b = _open_jpeg(res.images[0]).getpixel((20, 20))
    assert r > 200 and b < 60


def test_animated_webp_first_frame():
    frames = [Image.new("RGB", (40, 40), (0, 255, 0)), Image.new("RGB", (40, 40), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
    res = extract(out.getvalue(), None, None)
    r, g, b = _open_jpeg(res.images[0]).getpixel((20, 20))
    assert g > 200 and b < 60


def test_decompression_bomb_rejected():
    bomb = _img_bytes(Image.new("1", (8000, 8000)), "PNG")
    assert len(bomb) < 100_000
    with pytest.raises(BadFile, match="большая"):
        extract(bomb, "bomb.png", None)


def test_broken_image_is_badfile():
    jpeg = _img_bytes(Image.new("RGB", (300, 300), (1, 2, 3)), "JPEG")
    with pytest.raises(BadFile):
        extract(jpeg[:200], "p.jpg", None)
    with pytest.raises(BadFile):
        extract(b"not an image at all", "p.jpg", "image/jpeg")


def test_other_pillow_formats_not_opened():
    # BMP с именем .jpg: Pillow умеет BMP, но разбор ограничен четырьмя видами.
    bmp = _img_bytes(Image.new("RGB", (10, 10)), "BMP")
    assert sniff(bmp, "p.bmp", "image/bmp") is None
    with pytest.raises(Unsupported):
        extract(bmp, "p.jpg", "image/jpeg")
    # У TGA сигнатуры нет: решает имя .jpg, но Pillow открывает только четыре вида.
    tga = _img_bytes(Image.new("RGB", (10, 10)), "TGA")
    assert Image.open(io.BytesIO(tga)).format == "TGA"
    with pytest.raises(BadFile):
        extract(tga, "p.jpg", "image/jpeg")


# --- PDF ---------------------------------------------------------------------------------

def test_pdf_with_text():
    pages = [f"Page {i}: invoice number 12345, total amount due is 678 roubles. "
             "Payment is expected within ten business days of delivery." for i in range(3)]
    res = extract(_pdf(pages), "doc.pdf", "application/pdf")
    assert res.kind == "pdf" and res.pages == 3 and res.images == [] and not res.truncated
    assert "invoice number 12345" in res.text
    assert "Page 2" in res.text
    assert "\x00" not in res.text and "\r" not in res.text


def test_pdf_text_limits():
    pages = [f"Page {i} " + "lorem ipsum dolor sit amet " * 4 for i in range(10)]
    res = extract(_pdf(pages), "doc.pdf", None, max_pages=3)
    assert res.pages == 10 and res.truncated
    assert "Page 2" in res.text and "Page 3" not in res.text
    res = extract(_pdf(pages), "doc.pdf", None, max_text=50)
    assert res.truncated and len(res.text) <= 50 and res.images == []


def test_pdf_scan_rendered():
    res = extract(_scan_pdf(6), "scan.pdf", None, render_pages=2, image_side=500)
    assert res.kind == "pdf" and res.pages == 6 and res.text == ""
    assert len(res.images) == 2 and res.truncated
    for jpeg in res.images:
        img = _open_jpeg(jpeg)
        assert max(img.size) <= 500 and img.mode == "RGB"
    res = extract(_scan_pdf(2), "scan.pdf", None)
    assert len(res.images) == 2 and not res.truncated
    w, h = _open_jpeg(res.images[0]).size
    assert h == round(842 * ex.RENDER_DPI / 72) or abs(h - 842 * ex.RENDER_DPI / 72) <= 1
    assert h <= 1600


def test_pdf_short_text_counts_as_scan():
    res = extract(_pdf(["p. 1", "p. 2"]), "a.pdf", None)
    assert "p. 1" in res.text and len(res.images) == 2


def test_pdf_encrypted_is_badfile():
    data = _pdf(["secret contents of the document here"], password="secret")
    with pytest.raises(BadFile, match="парол"):
        extract(data, "locked.pdf", None)


def test_pdf_garbage_is_badfile():
    with pytest.raises(BadFile):
        extract(b"%PDF-1.7\n" + bytes(range(256)) * 10, "x.pdf", None)
    with pytest.raises(BadFile):
        extract(b"just text", "x.pdf", "application/pdf")


# --- docx --------------------------------------------------------------------------------

def test_docx_paragraphs_and_table():
    body = (
        _p("Договор поставки № 17")
        + "<w:p><w:r><w:t>Сумма:</w:t><w:tab/><w:t>100 000 ₽</w:t><w:br/>"
          "<w:t>Срок — до 1 ноября</w:t></w:r></w:p>"
        + "<w:p><w:del><w:r><w:delText>удалённое</w:delText></w:r></w:del></w:p>"
        + "<w:tbl>"
        + "<w:tr><w:tc>" + _p("Товар") + "</w:tc><w:tc>" + _p("Кол-во") + "</w:tc></w:tr>"
        + "<w:tr><w:tc>" + _p("Цемент") + "</w:tc><w:tc>" + _p("40") + "</w:tc></w:tr>"
        + "</w:tbl>"
        + "<w:sdt><w:sdtContent>" + _p("Подпись") + "</w:sdtContent></w:sdt>"
    )
    res = extract(_docx(body), "dogovor.docx", None)
    assert res.kind == "docx" and res.pages is None and res.images == [] and not res.truncated
    assert "Договор поставки № 17" in res.text
    assert "Сумма:\t100 000 ₽\nСрок — до 1 ноября" in res.text
    assert "Товар | Кол-во\nЦемент | 40" in res.text
    assert "Подпись" in res.text
    assert "удалённое" not in res.text and "\x00" not in res.text


def test_docx_truncated():
    body = "".join(_p(f"Абзац номер {i}") for i in range(500))
    res = extract(_docx(body), "a.docx", None, max_text=100)
    assert res.truncated and len(res.text) <= 100 and res.text.startswith("Абзац номер 0")


def test_docx_zip_bomb_rejected():
    payload = (f'<w:document {_W}><w:body>'.encode() + b" " * (20 * 1024 * 1024)
               + b"</w:body></w:document>")
    data = _docx("", document=payload)
    assert len(data) < 200_000
    with pytest.raises(BadFile, match="сжат"):
        extract(data, "bomb.docx", None)


def test_docx_total_size_limit(monkeypatch):
    monkeypatch.setattr(ex, "ZIP_MAX_TOTAL", 10_000)
    data = _docx(_p("x"), extra={"word/media/big.bin": b"\x01" * 20_000})
    with pytest.raises(BadFile, match="распакованном"):
        extract(data, "a.docx", None)


def test_docx_doctype_rejected():
    doc = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document {_W}>'
           f"<w:body>{_p('&a;')}</w:body></w:document>").encode()
    with pytest.raises(BadFile):
        extract(_docx("", document=doc), "a.docx", None)


def test_docx_encrypted_and_broken():
    enc = _ole("EncryptedPackage".encode("utf-16-le"))
    assert sniff(enc, "secret.docx", None) == "docx"
    with pytest.raises(BadFile, match="паролем"):
        extract(enc, "secret.docx", None)
    with pytest.raises(BadFile, match="паролем"):
        extract(enc, "secret.xlsx", None)
    good = _docx(_p("x"))
    with pytest.raises(BadFile):
        extract(good[: len(good) // 2], "half.docx", None)
    with pytest.raises(BadFile):
        extract(_docx("", document=b"<w:document><broken"), "a.docx", None)


# --- xlsx --------------------------------------------------------------------------------

def test_xlsx_two_sheets():
    import datetime as dt

    data = _xlsx({
        "Продажи": [["Объект", "Сумма", "Дата"], ["ЖК Река", 1500000.0, dt.date(2026, 9, 1)],
                    [None, 2.5, None], []],
        "Остатки": [["Квартира", 42], [True, "  пробелы  "]],
    })
    res = extract(data, "report.xlsx", None)
    assert res.kind == "xlsx" and res.pages == 2 and res.images == [] and not res.truncated
    assert "Лист: Продажи\nОбъект\tСумма\tДата\nЖК Река\t1500000\t2026-09-01\n2.5" in res.text
    assert "Лист: Остатки\nКвартира\t42\nда\tпробелы" in res.text


def test_xlsx_limits(monkeypatch):
    monkeypatch.setattr(ex, "XLSX_MAX_ROWS", 5)
    monkeypatch.setattr(ex, "XLSX_MAX_COLS", 3)
    data = _xlsx({"Лист1": [[f"r{i}", 1, 2, f"далеко{i}"] for i in range(20)]})
    res = extract(data, "a.xlsx", None)
    assert res.truncated
    assert "r4" in res.text and "r5" not in res.text and "далеко" not in res.text
    res = extract(_xlsx({"A": [[f"строка {i}"] for i in range(100)]}), "a.xlsx", None, max_text=40)
    assert res.truncated and len(res.text) <= 40


def test_xlsx_broken():
    data = _xlsx({"A": [[1]]})
    with pytest.raises(BadFile):
        extract(data[: len(data) // 2], "a.xlsx", None)
    zf_buf = io.BytesIO()
    with zipfile.ZipFile(zf_buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook>not really</workbook>")
    with pytest.raises(BadFile):
        extract(zf_buf.getvalue(), "a.xlsx", None)


# --- текст -------------------------------------------------------------------------------

def test_text_encodings():
    phrase = "Встреча в четверг в 15:00, адрес: ул. Ленина, 5"
    for data in (phrase.encode("utf-8"), phrase.encode("cp1251"), phrase.encode("utf-8-sig"),
                 phrase.encode("utf-16")):
        res = extract(data, "note.txt", None)
        assert (res.kind, res.text, res.pages, res.images, res.truncated) == (
            "text", phrase, None, [], False)


def test_text_cleanup_and_truncation():
    data = "строка один  \r\n\r\n\r\n\r\nстрока\x00 два\n".encode("utf-8")
    assert extract(data, "a.md", None).text == "строка один\n\nстрока два"
    long = ("Ж" * 1000).encode("utf-8")
    res = extract(long, "a.txt", None, max_text=100)
    assert res.truncated and res.text == "Ж" * 100
    # обрезка по байтам посреди символа UTF-8 не переводит файл в cp1251
    res = extract(("я" * 30 + "€" * 50).encode("utf-8"), "a.txt", None, max_text=10)
    assert res.text == "я" * 10 and res.truncated


def test_binary_named_txt_is_badfile():
    with pytest.raises(BadFile, match="текст"):
        extract(bytes(range(256)) * 8, "a.txt", None)


def test_only_two_exceptions_escape(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("неожиданное")

    monkeypatch.setattr(ex, "_docx", boom)
    with pytest.raises(BadFile):
        extract(_docx(_p("x")), "a.docx", None)

    def oom(*_a, **_k):
        raise MemoryError

    monkeypatch.setattr(ex, "_plain", oom)
    with pytest.raises(BadFile, match="памяти"):
        extract(b"hello", "a.txt", None)


def test_pdf_from_several_threads():
    # PDFium не потокобезопасен: разбор идёт под блокировкой, параллельные вызовы не мешают.
    from concurrent.futures import ThreadPoolExecutor

    text_pdf = _pdf(["Parallel extraction must give the same text every single time, ok."] * 2)
    scan = _scan_pdf(2)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda d: extract(d, "a.pdf", None), [text_pdf, scan] * 6))
    assert all(r.text == results[0].text and r.text for r in results[0::2])
    assert all(len(r.images) == 2 for r in results[1::2])
