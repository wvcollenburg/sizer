FROM python:3.12-slim

WORKDIR /app

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
