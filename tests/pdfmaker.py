"""Builds tiny real text-layer PDFs for tests (Helvetica, absolute positions), with no PDF library needed.

    make_pdf([page, page, ...])     each page is a list of (x, y_from_top, size, text)

Coordinates are in points on an A4 page (595 x 842). y is measured down from the top of the page, like pdfplumber's `top`.
"""
PAGE_W, PAGE_H = 595, 842


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[tuple]]) -> bytes:
    objects: list[bytes] = []                        # objects[i] is PDF object number i + 1

    def add(body: str | bytes) -> int:
        objects.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objects)

    def ops(item: tuple) -> str:
        x, y, size, text, *rest = item
        chunk = f"BT /F1 {size} Tf {x} {PAGE_H - y} Td ({_esc(text)}) Tj ET"
        if rest:
            chunk = f"{float(rest[0]):.3f} g\n{chunk}\n0 g"
        return chunk

    catalog = add("")                                # filled in below, once the page tree exists
    tree = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for items in pages:
        stream = "\n".join(ops(item) for item in items).encode("latin-1")
        content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        kids.append(add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"))
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objects[tree - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>").encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def flow(lines: list[str], x: float, top: float, size: float = 10, leading: float = 14):
    """Lines of text stacked from `top` downward. Returns (items, next_top)."""
    items = []
    for text in lines:
        items.append((x, top, size, text))
        top += leading
    return items, top
