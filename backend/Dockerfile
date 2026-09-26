FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY docker-entrypoint.sh .
# Normalize line endings at build time regardless of how the host
# checked this file out. A Windows clone with core.autocrlf=true can
# silently convert this script to CRLF even with .gitattributes
# present (existing working-tree files aren't retroactively
# re-normalized by a plain `git pull` -- only a fresh clone or an
# explicit re-checkout picks that up). A CRLF script breaks in two
# ways: the shebang line's interpreter path gets a trailing \r
# ("exec: no such file or directory"), and internal lines like
# `set -e` become `set -e\r`, which most shells reject as an illegal
# option. Stripping \r here is idempotent (a no-op on an
# already-LF file) and makes the image correct no matter what the
# build context contained.
RUN sed -i 's/\r$//' docker-entrypoint.sh && chmod +x docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
