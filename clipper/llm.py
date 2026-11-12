"""Free LLMs for short JSON answers: Gemini models first, Groq as a fallback."""
import json
import os
import re
import time

import requests


class LLM:
    def __init__(self, cfg: dict):
        self.chain = [("gemini", m) for m in cfg["llm"]["gemini"]]
        if os.getenv("GROQ_API_KEY"):
            self.chain.insert(1, ("groq", cfg["llm"]["groq"]))

    def json(self, prompt: str) -> dict:
        errors = []
        for provider, model in self.chain:
            for _ in range(2):
                try:
                    return _parse(getattr(self, "_" + provider)(prompt, model))
                except requests.HTTPError as e:
                    errors.append(f"{model}: {e.response.status_code if e.response is not None else '?'}")
                    if e.response is not None and e.response.status_code in (400, 401, 403, 404, 429):
                        break
                    time.sleep(5)
                except (requests.RequestException, ValueError, KeyError, IndexError) as e:
                    errors.append(f"{model}: {type(e).__name__}")
                    time.sleep(3)
        raise RuntimeError("LLM başarısız: " + "; ".join(errors[-4:]))

    def _gemini(self, prompt, model):
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, timeout=90,
                          json={"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _groq(self, prompt, model):
        r = requests.post("https://api.groq.com/openai/v1/chat/completions", timeout=90,
                          headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                          json={"model": model, "temperature": 0.7, "response_format": {"type": "json_object"},
                                "reasoning_effort": "low", "max_completion_tokens": 2000,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _parse(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
