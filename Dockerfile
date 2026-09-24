FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
COPY static ./static
RUN useradd --uid 10001 --create-home mesh && mkdir /app/data && chown mesh:mesh /app/data
USER mesh
EXPOSE 8080
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8080"]
