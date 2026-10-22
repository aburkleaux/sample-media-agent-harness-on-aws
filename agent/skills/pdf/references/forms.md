**CRITICAL: Complete these steps in order.**

Check if the PDF has fillable form fields first. Run in code_interpreter:

```python
from pypdf import PdfReader
reader = PdfReader("/tmp/form.pdf")
fields = reader.get_fields()
if fields:
    print(f"Found {len(fields)} fillable fields:")
    for name, field in fields.items():
        print(f"  {name}: {field.get('/FT', 'Unknown')}")
else:
    print("No fillable fields found.")
```

# Fillable Fields

1. Extract field info:
```python
import json
from pypdf import PdfReader
reader = PdfReader("/tmp/form.pdf")
fields = reader.get_fields() or {}
result = []
for name, field in fields.items():
    entry = {"field_id": name, "type": "text"}
    ft = str(field.get("/FT", ""))
    if "Btn" in ft:
        entry["type"] = "checkbox"
        entry["checked_value"] = "/On"
        entry["unchecked_value"] = "/Off"
    result.append(entry)
print(json.dumps(result, indent=2))
```

2. Fill the form:
```python
from pypdf import PdfReader, PdfWriter
reader = PdfReader("/tmp/form.pdf")
writer = PdfWriter()
writer.append(reader)
writer.update_page_form_field_values(writer.pages[0], {"field_name": "value"})
with open("/tmp/filled.pdf", "wb") as f:
    writer.write(f)
```

# Non-Fillable Fields

For non-fillable PDFs, add text annotations using reportlab overlay:

```python
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from io import BytesIO

reader = PdfReader("/tmp/form.pdf")
writer = PdfWriter()
page = reader.pages[0]

buf = BytesIO()
c = canvas.Canvas(buf, pagesize=letter)
c.setFont("Helvetica", 10)
c.drawString(100, 700, "John Doe")  # x, y, text
c.save()
buf.seek(0)

overlay = PdfReader(buf)
page.merge_page(overlay.pages[0])
writer.add_page(page)

with open("/tmp/filled.pdf", "wb") as f:
    writer.write(f)
```
