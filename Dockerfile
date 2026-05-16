FROM python:3.10
RUN apt-get update && apt-get install -y ffmpeg
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
CMD ["python", "app.py"]
