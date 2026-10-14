FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
WORKDIR /app
RUN pip install --no-cache-dir --no-deps torch==2.8.0+cpu --index-url https://download.pytorch.org/whl/cpu
COPY seaice-requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /app/requirements.txt
COPY service.py /app/service.py
RUN useradd --uid 10001 --no-create-home researcher
USER researcher
ENV MODEL_PATH=/models/model.pt
EXPOSE 8080
CMD ["uvicorn", "service:factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
