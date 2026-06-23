FROM python:3.12-slim

WORKDIR /app

# Runtime libs for the exports:
#  * cairosvg rasterises the cluster diagram SVG → PNG (cairo/pango).
#  * libreoffice-writer converts the authored .docx proposal → PDF (headless).
#  * libreoffice-impress provides the PowerPoint filters for the deck → PDF
#    ("Slides PDF"); Writer alone can't convert .pptx.
#  * fonts-liberation gives Arial-compatible glyphs for all of them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 libpango-1.0-0 libpangocairo-1.0-0 fonts-liberation \
        libreoffice-writer libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

# Branded PPTX/PDF export template. Lives outside app/ and is read at
# ../resources/template.pptx relative to the app code, so copy it to /resources.
COPY resources/ /resources/

# Install the Martel Sans family system-wide so LibreOffice renders the branded
# slide titles ("Martel Sans ExtraLight") correctly when converting the deck →
# PDF. Without it LibreOffice substitutes the font and mangles the title spacing.
RUN mkdir -p /usr/share/fonts/truetype/martel-sans \
    && cp /resources/fonts/*.ttf /usr/share/fonts/truetype/martel-sans/ \
    && fc-cache -f

EXPOSE 5000

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
