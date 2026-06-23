FROM python:3.12-slim

WORKDIR /app

# Runtime libs for the exports:
#  * cairosvg rasterises the cluster diagram SVG → PNG (cairo/pango).
#  * libreoffice-writer converts the authored .docx proposal → PDF (headless).
#  * fonts-liberation gives Arial-compatible glyphs for both.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 fonts-liberation \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Branded PPTX/PDF export template. Lives outside app/ and is read at
# ../resources/template.pptx relative to the app code, so copy it to /resources.
COPY resources/ /resources/

EXPOSE 5000

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
