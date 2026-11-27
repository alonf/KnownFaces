FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 KF_DATA=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY knownfaces/__init__.py knownfaces/models.py ./knownfaces/
RUN python -m knownfaces.models
RUN useradd --uid 10001 --create-home knownfaces && mkdir /data && chown knownfaces /data
COPY knownfaces/ ./knownfaces/
USER knownfaces
EXPOSE 8093
CMD ["uvicorn","knownfaces.app:create_app","--factory","--host","0.0.0.0","--port","8093"]
