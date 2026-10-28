FROM python:3.12-slim
WORKDIR /app

RUN adduser --disabled-password --gecos "" myuser
USER myuser
ENV PATH="/home/myuser/.local/bin:$PATH"

COPY --chown=myuser:myuser agent/ /app/

RUN pip install --user -r /app/requirements.txt

EXPOSE 8080

CMD ["python", "main.py"]
