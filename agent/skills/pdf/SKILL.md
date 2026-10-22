---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files or convert documents to PDF. This includes reading or extracting text/tables, merging, splitting, rotating, watermarks, creating new PDFs, filling forms, encrypting/decrypting, extracting images, OCR, and converting Word (.docx) or PowerPoint (.pptx) to PDF. Use code_interpreter for all operations and artifact tool for file storage.
---

# PDF Processing Guide

## Overview

Process PDFs using the code_interpreter (sandboxed Python) and store results in S3 via the artifact tool.
For advanced features see [references/reference.md](references/reference.md).
For form filling see [references/forms.md](references/forms.md).

## Architecture

1. **code_interpreter** — Run Python code for all PDF operations (libraries are pre-installed)
2. **artifact tool** — Return the permanent URL of a file you wrote to the session output prefix
   (`operation="adopt"`), or list/download existing artifacts
3. **Scripts in `scripts/`** — Reference code to adapt, not run directly

## Available Libraries (pre-installed in code interpreter)

`pypdf`, `PyPDF2`, `pdfplumber`, `reportlab`, `pypdfium2`, `pdfminer.six`, `pdf2image`, `tabula-py`, `fpdf`, `boto3`

## Workflow Pattern

For every PDF task that produces a file, in ONE code_interpreter call:

1. Create the PDF at a `/tmp` path.
2. Upload it with boto3 DIRECTLY to this session's **OUTPUT prefix** — the exact
   `s3://<bucket>/<output-prefix><name>.pdf` target stated in your instructions: the
   **SESSION OUTPUT LOCATION** section in a chat, or the **S3 CONTEXT** section in a sub-agent or
   plan node. Use that bucket, prefix and region verbatim — do NOT hardcode a bucket, account id
   or region, and do NOT invent a prefix. (The prefix is either `artifacts/<session>/output/` or
   `projects/<project>/sessions/<session>/output/`, depending on the environment; copy it, never
   build it.)
3. Optionally call `artifact(operation="adopt", key="<name>.pdf")` — it returns the permanent URL
   and fixes the ContentType. A direct write to the output prefix is already durable.
4. Return the URL to the user.

**Never write outside the output prefix.** The code sandbox's S3 credentials are scoped to this
session: a flat `artifacts/<name>.pdf` key, a guessed session path, or another session's prefix is
denied with `AccessDenied`. If you are unsure of the target, call
`artifact(operation="adopt", key="<name>.pdf")` first — when the object isn't there yet it returns
the exact `s3://<bucket>/<key>` write target for this session.

**CRITICAL:**
- Upload the file to S3 from inside code_interpreter with boto3 — never pass base64 file content
  as a tool argument (it is truncated).
- Create the boto3 client in the SAME cell that uses it (`boto3.client("s3", region_name=...)`).
  A client kept from an earlier cell can hold credentials that have since been rotated.

## Output versioning (IMPORTANT)

When you MODIFY an existing artifact — add a cover page, watermark, merge,
split, rotate, encrypt, convert, etc. — write the result to a **NEW, uniquely
named S3 key**. Do NOT overwrite the input file or a previous output.

- Include a short descriptor + a uniqueness token, e.g.
  `<output-prefix>meridian_with_cover_1a2b3c4d.pdf` (use `uuid.uuid4().hex[:8]`),
  not `<output-prefix>meridian.pdf` again.
- Why: (1) it preserves the earlier version so the user keeps their history,
  and (2) a NEW url guarantees the modified file re-renders — reusing the same
  key can serve the stale (pre-edit) copy from the browser/CDN cache.
- Always surface the new key/URL in your reply so the user can see the new
  version rendered.

## Quick Start — Create and Upload a PDF

```python
# Run in code_interpreter — creates the PDF and uploads it to the session output prefix.
import boto3
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

# Create PDF
doc = SimpleDocTemplate("/tmp/report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
doc.build([Paragraph("Hello World", styles['Title'])])

# Upload DIRECTLY to the session OUTPUT prefix — copy bucket, prefix and region verbatim from
# your SESSION OUTPUT LOCATION / S3 CONTEXT section (placeholders below; never hardcode them).
bucket = "<bucket from your instructions>"
prefix = "<output prefix from your instructions>"   # ends with "/output/"
region = "<region from your instructions>"
key = f"{prefix}report.pdf"
boto3.client("s3", region_name=region).upload_file(
    "/tmp/report.pdf", bucket, key, ExtraArgs={"ContentType": "application/pdf"})
print("Uploaded to", key)
```

Then (optionally) call `artifact(operation="adopt", key="report.pdf")` to get the permanent URL.

## Common Operations

### Extract Text

```python
import pdfplumber
# File must be downloaded from S3 first via boto3 if not already in sandbox
with pdfplumber.open("/tmp/input.pdf") as pdf:
    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
print(text)
```

### Extract Tables

```python
import pdfplumber, json
with pdfplumber.open("/tmp/input.pdf") as pdf:
    tables = []
    for page in pdf.pages:
        for table in page.extract_tables():
            tables.append(table)
print(json.dumps(tables))
```

### Merge PDFs

```python
from pypdf import PdfWriter, PdfReader
writer = PdfWriter()
for path in ["/tmp/doc1.pdf", "/tmp/doc2.pdf"]:
    for page in PdfReader(path).pages:
        writer.add_page(page)
with open("/tmp/merged.pdf", "wb") as f:
    writer.write(f)
```

### Split PDF

```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("/tmp/input.pdf")
for i, page in enumerate(reader.pages):
    w = PdfWriter()
    w.add_page(page)
    with open(f"/tmp/page_{i+1}.pdf", "wb") as f:
        w.write(f)
```

### Rotate Pages

```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("/tmp/input.pdf")
writer = PdfWriter()
page = reader.pages[0]
page.rotate(90)
writer.add_page(page)
with open("/tmp/rotated.pdf", "wb") as f:
    writer.write(f)
```

### Add Watermark

```python
from pypdf import PdfReader, PdfWriter
watermark = PdfReader("/tmp/watermark.pdf").pages[0]
reader = PdfReader("/tmp/input.pdf")
writer = PdfWriter()
for page in reader.pages:
    page.merge_page(watermark)
    writer.add_page(page)
with open("/tmp/watermarked.pdf", "wb") as f:
    writer.write(f)
```

### Password Protection

```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("/tmp/input.pdf")
writer = PdfWriter()
for page in reader.pages:
    writer.add_page(page)
writer.encrypt("userpass", "ownerpass")
with open("/tmp/encrypted.pdf", "wb") as f:
    writer.write(f)
```

### Create Report with Table

```python
import boto3
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

doc = SimpleDocTemplate("/tmp/report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
data = [["Product", "Price", "Qty"], ["Widget", "$50", "100"], ["Gadget", "$75", "50"]]
table = Table(data)
table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
    ("GRID", (0, 0), (-1, -1), 1, colors.black),
]))
doc.build([Paragraph("Sales Report", styles["Title"]), Spacer(1, 12), table])

# Upload DIRECTLY to the session OUTPUT prefix — copy bucket, prefix and region verbatim from
# your SESSION OUTPUT LOCATION / S3 CONTEXT section (placeholders below; never hardcode them).
bucket = "<bucket from your instructions>"
prefix = "<output prefix from your instructions>"   # ends with "/output/"
region = "<region from your instructions>"
key = f"{prefix}report.pdf"
boto3.client("s3", region_name=region).upload_file(
    "/tmp/report.pdf", bucket, key, ExtraArgs={"ContentType": "application/pdf"})
print("Uploaded to", key)
```

Then (optionally) call `artifact(operation="adopt", key="report.pdf")` to get the permanent URL.

**Important**: Never use Unicode subscript/superscript characters in ReportLab. Use `<sub>` and `<super>` XML tags instead.

## Reading Files from S3 in Code Interpreter

The input file's `s3://<bucket>/<key>` URI is given to you in the prompt — parse it, do not guess
a bucket:

```python
import boto3
uri = "s3://<bucket>/<key>"              # the exact s3:// URI from your prompt
bucket, key = uri[len("s3://"):].split("/", 1)
boto3.client("s3").download_file(bucket, key, "/tmp/input.pdf")
```

## Converting Other Formats to PDF

### Word (.docx) → PDF

```python
from docx import Document
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

doc = Document("/tmp/input.docx")
styles = getSampleStyleSheet()
elements = []

for para in doc.paragraphs:
    if para.style.name.startswith("Heading"):
        elements.append(Paragraph(para.text, styles["Heading1"]))
    elif para.text.strip():
        elements.append(Paragraph(para.text, styles["Normal"]))
        elements.append(Spacer(1, 6))

pdf = SimpleDocTemplate("/tmp/output.pdf", pagesize=letter)
pdf.build(elements)
```

Note: This preserves text and headings but loses complex formatting (images, tables, columns). For tables, extract with `doc.tables` and rebuild with reportlab `Table`.

### PowerPoint (.pptx) → PDF

```python
from pptx import Presentation
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfgen import canvas

prs = Presentation("/tmp/input.pptx")
c = canvas.Canvas("/tmp/output.pdf", pagesize=landscape(letter))
w, h = landscape(letter)

for slide in prs.slides:
    texts = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                if para.text.strip():
                    texts.append(para.text)
    y = h - 72
    for text in texts:
        if y < 72:
            c.showPage()
            y = h - 72
        c.drawString(72, y, text)
        y -= 18
    c.showPage()

c.save()
```

Note: Extracts text only. Complex slides with images, charts, and layouts will lose visual fidelity.

### Supported Upload Formats

| Format | Extension | Quality |
|--------|-----------|---------|
| Word | .docx | Text + headings preserved, formatting approximate |
| PowerPoint | .pptx | Text extracted per slide, layout lost |
| HTML | .html | Good via `pdfkit` |
| Markdown | .md | Good via `pypandoc` |
| Plain text | .txt | Exact via reportlab |

## Quick Reference

| Task | Library | Notes |
|------|---------|-------|
| Extract text | pdfplumber | `page.extract_text()` |
| Extract tables | pdfplumber | `page.extract_tables()` |
| Create PDFs | reportlab | Platypus for complex layouts |
| Merge/split | pypdf | `PdfWriter` / `PdfReader` |
| Rotate | pypdf | `page.rotate(degrees)` |
| OCR scanned | pdf2image + external | Convert to images first |
| Metadata | pypdf | `reader.metadata` |
