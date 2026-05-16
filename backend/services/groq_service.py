"""
services/groq_service.py

Integración con Groq para visión (llama-4-scout-17b).
Groq es más rápido que OpenRouter — se usa PRIMERO en la rotación de visión.
Fallback a OpenRouter si Groq falla o no está configurado.
"""

import os
import json
import logging
import requests
from typing import Optional

logger = logging.getLogger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Límite de Groq para base64: 4MB por request
GROQ_MAX_BASE64_BYTES = 4 * 1024 * 1024


class GroqService:
    """
    Servicio de visión usando Groq (llama-4-scout).
    API compatible con OpenAI — mismo formato que OpenRouter.
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.disponible = bool(self.api_key) and self.api_key != "TU_GROQ_API_KEY_AQUI"
        self.llamadas = 0
        self.errores = 0

        if not self.disponible:
            logger.info("GROQ_API_KEY no configurada — Groq deshabilitado.")
        else:
            logger.info("Groq Vision disponible — llama-4-scout activo.")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _imagen_dentro_del_limite(self, base64_img: str) -> bool:
        """Groq acepta máx 4MB de imagen base64."""
        return len(base64_img.encode()) <= GROQ_MAX_BASE64_BYTES

    def llamar_vision(
        self,
        prompt: str,
        base64_img: str,
        mime_type: str,
        max_tokens: int = 400,
    ) -> Optional[str]:
        """
        Llama a llama-4-scout con una imagen base64.
        Retorna el texto de la respuesta o None si falla.
        """
        if not self.disponible:
            return None

        if not self._imagen_dentro_del_limite(base64_img):
            print(f"[Groq] Imagen demasiado grande (>4MB) — saltando Groq")
            return None

        try:
            resp = requests.post(
                f"{GROQ_BASE_URL}/chat/completions",
                headers=self._headers(),
                json={
                    "model": GROQ_VISION_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Responde SOLO con JSON válido. Sin markdown. Sin explicaciones. Sin texto antes ni después del JSON.",
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:{mime_type};base64,{base64_img}"
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        },
                    ],
                },
                timeout=30,
            )

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices")
                if not choices:
                    print(f"[Groq] Sin choices en respuesta")
                    self.errores += 1
                    return None
                content = (choices[0].get("message") or {}).get("content")
                if not content:
                    print(f"[Groq] content es null/vacío")
                    self.errores += 1
                    return None
                self.llamadas += 1
                print(f"[Groq OK] {content[:120]}")
                return content.strip()

            elif resp.status_code == 429:
                print(f"[Groq] Rate limit (429) — pasando a OpenRouter")
                self.errores += 1
                return None
            elif resp.status_code == 413:
                print(f"[Groq] Imagen demasiado grande (413) — pasando a OpenRouter")
                self.errores += 1
                return None
            else:
                body = resp.text[:200]
                print(f"[Groq ERROR] HTTP {resp.status_code}: {body}")
                self.errores += 1
                return None

        except Exception as e:
            print(f"[Groq EXCEPTION] {e}")
            self.errores += 1
            return None

    def resumen(self) -> dict:
        return {
            "disponible": self.disponible,
            "modelo": GROQ_VISION_MODEL,
            "llamadas": self.llamadas,
            "errores": self.errores,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
_groq_instance: Optional[GroqService] = None


def get_groq_service() -> GroqService:
    global _groq_instance
    if _groq_instance is None:
        _groq_instance = GroqService(api_key=os.getenv("GROQ_API_KEY", ""))
    return _groq_instance
