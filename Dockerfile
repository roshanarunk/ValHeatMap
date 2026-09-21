# ValHeatMap: API, crawler and built frontend in one image.
#
# The frontend is built in a separate stage so Node never ships to the
# runtime image -- it is only needed to produce static files.

FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim
WORKDIR /app

# Only the server requirements: pystray and pillow are for the desktop
# tray icon and would fail to install (and be useless) here.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --quiet -r requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist

# The database lives on a mounted volume, so it survives deploys and is
# shared between the API and the crawler.
ENV VALHEATMAP_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
EXPOSE 8000

# supervisord would be another dependency for two processes; a small
# shell supervisor is enough and keeps the image minimal.
COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
