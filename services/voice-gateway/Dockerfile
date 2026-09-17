FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py llm.py tools.py tts.py agent.py context.py session.py      contracts.py storage.py devices.py coordinator.py ha_executor.py      ma_reader.py playback_state.py scene_catalog.py catalog_resolver.py      api_v1.py auth.py health.py      migrations/      /app/

EXPOSE 8200
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200"]
