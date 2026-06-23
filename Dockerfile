FROM python:3.11-slim

WORKDIR /app

# 依赖先装（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 源码
COPY . .

# 数据目录（SQLite 文件放这里）
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
