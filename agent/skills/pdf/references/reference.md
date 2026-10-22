# PDF Processing Advanced Reference

All code runs in code_interpreter. Libraries are pre-installed.

## pypdfium2 — Fast Rendering

```python
import pypdfium2 as pdfium
pdf = pdfium.PdfDocument("/tmp/doc.pdf")
for i, page in enumerate(pdf):
    bitmap = page.render(scale=2.0)
    img = bitmap.to_pil()
    img.save(f"/tmp/page_{i+1}.png", "PNG")
```

## Advanced pdfplumber — Text with Coordinates

```python
import pdfplumber
with pdfplumber.open("/tmp/doc.pdf") as pdf:
    page = pdf.pages[0]
    for char in page.chars[:10]:
        print(f"'{char['text']}' at x:{char['x0']:.1f} y:{char['y0']:.1f}")
    bbox_text = page.within_bbox((100, 100, 400, 200)).extract_text()
```

## Advanced reportlab — Tables with Styling

```python
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors

data = [["Product", "Q1", "Q2"], ["Widgets", "120", "135"], ["Gadgets", "85", "92"]]
doc = SimpleDocTemplate("/tmp/report.pdf")
table = Table(data)
table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
    ("GRID", (0, 0), (-1, -1), 1, colors.black),
]))
doc.build([Paragraph("Report", getSampleStyleSheet()["Title"]), table])
```

## S3 File Access from Code Interpreter

Take the input URI from your prompt and the output bucket/prefix from your S3 CONTEXT — do NOT
hardcode a bucket or region.

```python
import boto3
s3 = boto3.client("s3")
# Download an input named in your prompt (parse its s3:// URI)
in_uri = "s3://<bucket>/<key>"
b, k = in_uri[len("s3://"):].split("/", 1)
s3.download_file(b, k, "/tmp/input.pdf")
# Upload the result to this session's output prefix (values from your S3 CONTEXT)
s3.upload_file("/tmp/output.pdf", "<output bucket from S3 CONTEXT>", "<session OUTPUT prefix from S3 CONTEXT>output.pdf")
```

## Performance Tips

1. Use `pdfplumber` for tables, `pypdf` for merge/split
2. Process large PDFs in chunks
3. Use `/tmp/` for all intermediate files in code interpreter

## Troubleshooting

### Encrypted PDFs
```python
from pypdf import PdfReader
reader = PdfReader("/tmp/encrypted.pdf")
if reader.is_encrypted:
    reader.decrypt("password")
```

### Text Extraction Fails → OCR Fallback
```python
from pdf2image import convert_from_path
# Note: OCR requires tesseract which may not be in code interpreter
# Use pdf2image to convert to images for visual analysis instead
images = convert_from_path("/tmp/scanned.pdf")
images[0].save("/tmp/page1.png")
```

## License Info

| Library | License |
|---------|---------|
| pypdf | BSD |
| pdfplumber | MIT |
| reportlab | BSD |
| pypdfium2 | Apache/BSD |
