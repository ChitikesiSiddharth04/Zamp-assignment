# Northgate AP — invoice-to-decision process.
# Docker guarantees Tesseract OCR installs correctly, which the native Python
# runtime does not: it's a system binary, not a pip package, and the scanned-
# invoice edge case depends on it being present.

FROM python:3.11-slim

# Tesseract for OCR, plus the render libs pymupdf needs to rasterize PDF pages.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate the sample invoices and master data at build time so the container
# starts ready to demo — no first-run delay.
RUN python scripts/make_samples.py

EXPOSE 8080
ENV AP_STAGE_DELAY_MS=0

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
