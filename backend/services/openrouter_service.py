"""
services/openrouter_service.py

Integración con OpenRouter para usar modelos GRATIS.
Soporta modelos de visión para extracción de precios desde imágenes.
Patrón idéntico al proyecto Argos: rotación automática con fallback.
"""

import os
import json
import time
import logging
import requests
from typing import Any, Optional

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ── Modelos de visión (leen imágenes + texto) ─────────────────────────────────
# IDs verificados en OpenRouter — actualizar si cambian (verificado mayo 2026)
VISION_FREE_MODELS = [
    "baidu/qianfan-ocr-fast:free",               # ✅ CONFIRMADO: lee precios y barcodes correctamente
    "google/gemma-4-31b-it:free",                # ⚠️ soporta visión — se rate-limita seguido
    "google/gemma-4-26b-a4b-it:free",            # ⚠️ soporta visión — se rate-limita seguido
    # nvidia/nemotron-nano-12b-v2-vl:free → REMOVIDO: siempre devuelve content=null (bug del modelo)
    # tencent/hy3-preview:free → REMOVIDO: 404 "No endpoints found that support image input"
]

# ── Modelos de texto (normalización, recomendaciones) ─────────────────────────
PREFERRED_FREE_MODELS = [
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
]

BACKUP_FREE_MODELS = [
    "openai/gpt-oss-20b:free",
    "qwen/qwen3-next-80b-a3b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
    "minimax/minimax-m2.5:free",
]

LIGHTWEIGHT_FREE_MODELS = [
    "meta-llama/llama-3.2-3b-instruct:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "google/gemma-4-26b-a4b-it:free",
]

# Cache de modelos disponibles (TTL 5 minutos)
_models_cache: list = []
_models_cache_ts: float = 0
_CACHE_TTL = 300


class OpenRouterService:
    """
    Servicio de IA gratuita usando OpenRouter.
    - Rotación automática de modelos con fallback
    - Soporte de visión para etiquetas de supermercado
    - Prompts compactos para minimizar tokens
    """

    def __init__(self, api_key: str = None, max_models_to_try: int = 3):
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.max_models_to_try = max_models_to_try
        self.llamadas_totales = 0
        self.errores_totales = 0
        self.disponible = bool(self.api_key)

        if not self.disponible:
            logger.warning("OPENROUTER_API_KEY no configurada. Se usará OCR como fallback.")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _get_available_models(self) -> list:
        global _models_cache, _models_cache_ts

        if _models_cache and (time.time() - _models_cache_ts) < _CACHE_TTL:
            return _models_cache

        try:
            resp = requests.get(
                f"{OPENROUTER_BASE_URL}/models",
                headers=self._headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                all_models = resp.json().get("data", [])
                _models_cache = [m["id"] for m in all_models if m.get("id", "").endswith(":free")]
                _models_cache_ts = time.time()
                logger.info(f"{len(_models_cache)} modelos :free disponibles en OpenRouter")
                return _models_cache
        except Exception as e:
            logger.warning(f"No se pudo consultar modelos OpenRouter: {e}")

        return PREFERRED_FREE_MODELS + BACKUP_FREE_MODELS

    def _build_rotation_list(self, include_vision: bool = False) -> list:
        available = set(self._get_available_models())
        priority = (VISION_FREE_MODELS if include_vision else []) + PREFERRED_FREE_MODELS + BACKUP_FREE_MODELS + LIGHTWEIGHT_FREE_MODELS

        ordered = []
        for model_id in priority:
            if model_id in available and model_id not in ordered:
                ordered.append(model_id)

        for model_id in available:
            if model_id not in ordered:
                ordered.append(model_id)

        return ordered[: self.max_models_to_try]

    def _llamar_modelo(self, model_id: str, prompt: str, max_tokens: int = 300) -> Optional[str]:
        """Llama a un modelo de texto. Retorna el texto o None si falla."""
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers(),
                json={
                    "model": model_id,
                    "max_tokens": max_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Responde SOLO con JSON válido. Sin markdown. Sin explicaciones.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=30,
            )

            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                self.llamadas_totales += 1
                return content.strip()
            else:
                logger.debug(f"Modelo {model_id} devolvió {resp.status_code}")
                return None

        except Exception as e:
            logger.debug(f"Error con {model_id}: {e}")
            self.errores_totales += 1
            return None

    def _llamar_modelo_vision(
        self,
        model_id: str,
        prompt: str,
        base64_img: str,
        mime_type: str,
        max_tokens: int = 400,
    ) -> Optional[str]:
        """Llama a un modelo de visión con imagen + texto."""
        try:
            resp = requests.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=self._headers(),
                json={
                    "model": model_id,
                    "max_tokens": max_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": "Responde SOLO con JSON válido. Sin markdown. Sin explicaciones.",
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
                timeout=45,
            )

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices")
                if not choices:
                    print(f"[OpenRouter EMPTY] {model_id} → sin choices en respuesta")
                    self.errores_totales += 1
                    return None
                content = (choices[0].get("message") or {}).get("content")
                if not content:
                    print(f"[OpenRouter NULL] {model_id} → content es null/vacío")
                    self.errores_totales += 1
                    return None
                self.llamadas_totales += 1
                print(f"[OpenRouter OK] {model_id} → {content[:120]}")
                return content.strip()
            else:
                body = resp.text[:300]
                print(f"[OpenRouter ERROR] {model_id} → HTTP {resp.status_code}: {body}")
                self.errores_totales += 1
                return None

        except Exception as e:
            print(f"[OpenRouter EXCEPTION] {model_id}: {e}")
            self.errores_totales += 1
            return None

    def preguntar_con_rotacion(
        self,
        prompt: str,
        max_tokens: int = 300,
        esperar_json: bool = True,
    ) -> dict:
        """Texto con rotación automática de modelos."""
        if not self.disponible:
            return {"exito": False, "razon": "Sin API key OpenRouter", "respuesta": None}

        modelos = self._build_rotation_list(include_vision=False)
        errores = []

        for model_id in modelos:
            texto = self._llamar_modelo(model_id, prompt, max_tokens)

            if texto:
                if esperar_json:
                    clean = texto
                    if "```" in clean:
                        for part in clean.split("```"):
                            if part.startswith("json"):
                                clean = part[4:].strip()
                                break
                            elif "{" in part or "[" in part:
                                clean = part.strip()
                    try:
                        parsed = json.loads(clean)
                        return {"exito": True, "modelo_usado": model_id, "respuesta": parsed}
                    except json.JSONDecodeError:
                        errores.append({"model": model_id, "error": "JSON inválido"})
                        continue
                else:
                    return {"exito": True, "modelo_usado": model_id, "respuesta": texto}

            errores.append({"model": model_id, "error": "Sin respuesta"})

        return {"exito": False, "razon": "Ningún modelo respondió", "errores": errores, "respuesta": None}

    def preguntar_vision_con_rotacion(
        self,
        prompt: str,
        base64_img: str,
        mime_type: str,
        max_tokens: int = 400,
    ) -> dict:
        """
        Visión con rotación automática.
        Orden de prioridad:
          1. Groq (llama-4-scout) — más rápido, tu API key propia
          2. OpenRouter modelos de visión — fallback si Groq falla / rate limit
        """
        errores = []

        # ── 1. Intentar Groq primero ──────────────────────────────────────────
        from services.groq_service import get_groq_service
        groq = get_groq_service()
        if groq.disponible:
            texto = groq.llamar_vision(prompt, base64_img, mime_type, max_tokens)
            if texto:
                clean = texto
                if "```" in clean:
                    for part in clean.split("```"):
                        if part.startswith("json"):
                            clean = part[4:].strip()
                            break
                        elif "{" in part:
                            clean = part.strip()
                try:
                    parsed = json.loads(clean)
                    return {"exito": True, "modelo_usado": "groq/llama-4-scout", "respuesta": parsed}
                except json.JSONDecodeError:
                    errores.append({"model": "groq/llama-4-scout", "error": "JSON inválido", "texto": texto[:200]})
            else:
                errores.append({"model": "groq/llama-4-scout", "error": "Sin respuesta"})

        # ── 2. Fallback: OpenRouter modelos de visión ─────────────────────────
        if not self.disponible:
            return {"exito": False, "razon": "Sin API key OpenRouter ni Groq respondió", "errores": errores, "respuesta": None}

        available = set(self._get_available_models())
        modelos = [m for m in VISION_FREE_MODELS if m in available]
        if not modelos:
            modelos = VISION_FREE_MODELS  # Intentar igual si el caché falló
        modelos = modelos[: self.max_models_to_try]

        for model_id in modelos:
            texto = self._llamar_modelo_vision(model_id, prompt, base64_img, mime_type, max_tokens)

            if texto:
                clean = texto
                if "```" in clean:
                    for part in clean.split("```"):
                        if part.startswith("json"):
                            clean = part[4:].strip()
                            break
                        elif "{" in part:
                            clean = part.strip()
                try:
                    parsed = json.loads(clean)
                    return {"exito": True, "modelo_usado": model_id, "respuesta": parsed}
                except json.JSONDecodeError:
                    errores.append({"model": model_id, "error": "JSON inválido", "texto": texto[:200]})
                    continue

            errores.append({"model": model_id, "error": "Sin respuesta"})

        return {"exito": False, "razon": "Ningún modelo de visión respondió", "errores": errores, "respuesta": None}

    # ── Métodos específicos del supermercado ──────────────────────────────────

    # Pistas por tienda para que el LLM no confunda puntos de fidelidad con precios
    _STORE_HINTS: dict = {
        "exito": (
            'En Éxito: '
            '"llevalo por X puntos" son puntos de fidelidad, NUNCA el precio. '
            '"GRAMO a $X.XX" y "MILILITRO a $X.XX" son precios por unidad de medida — IGNORAR completamente. '
            'El precio de venta es el número grande con $ al centro-izquierda de la etiqueta. '
            'Etiqueta rosada/morada con "% dcto" y "Antes $X": '
            'precio_regular = valor de "Antes", precio_con_descuento = precio grande visible.'
        ),
        "or":       'En OR, "Llévalo por $X" SÍ es el precio. "llevalo por X puntos" son puntos de fidelidad, no el precio.',
        "euro":     '"llevalo por X puntos" son puntos de fidelidad, NUNCA el precio.',
        "jumbo":    '"llevalo por X puntos" son puntos de fidelidad, NUNCA el precio.',
        "carulla":  '"llevalo por X puntos" son puntos de fidelidad, NUNCA el precio.',
    }

    def extraer_precio_imagen(self, base64_img: str, mime_type: str, store: str = "") -> dict:
        """
        Extrae información de precio desde una imagen de etiqueta.
        Retorna dict estructurado con producto, precio, descuento y confianza.
        Acepta store para dar contexto al LLM sobre el formato de la tienda.
        """
        store_hint = self._STORE_HINTS.get(store.lower(), "")
        if store_hint:
            store_hint = f"\nCONTEXTO TIENDA ({store}): {store_hint}"

        prompt = (
            "Analiza esta etiqueta de precio de supermercado colombiano."
            f"{store_hint}\n"
            "REGLAS IMPORTANTES:\n"
            "1. En Colombia el PUNTO es separador de miles: $38.650=38650 pesos, $24.990=24990 pesos. NUNCA uses punto como decimal.\n"
            "2. Líneas como 'ML A $X', 'G A $X', 'GRAMO a $X', 'MILILITRO a $X', '$X/ML' son precio por unidad de medida — IGNORAR completamente, NO es el precio de venta.\n"
            "3. El precio de venta es el número más grande y prominente de la etiqueta blanca superior.\n"
            "4. Si la etiqueta inferior muestra un número de DOS cifras (ej: 25, 30, 15), es un PORCENTAJE de descuento. Calcula: precio_con_descuento = redondear(precio_regular * (1 - porcentaje/100)). Pon ese resultado en precio_con_descuento y precio_que_se_paga.\n"
            "5. Si la etiqueta inferior muestra un precio completo (más de 3 cifras), ese es directamente precio_con_descuento.\n"
            "Extrae SOLO este JSON con valores enteros para precios (sin decimales, sin puntos):\n"
            '{"producto":"nombre","precio_regular":38650,'
            '"tiene_descuento":false,"precio_con_descuento":null,'
            '"precio_que_se_paga":38650,"unidad":"ml|g|kg|lt|und",'
            '"volumen_ml":null,"cantidad":null,"codigo_barras":null,"plu":null,'
            '"fecha_promo_inicio":"DD/MM/YYYY o null","fecha_promo_fin":"DD/MM/YYYY o null",'
            '"confianza":0.9}'
        )

        resultado = self.preguntar_vision_con_rotacion(prompt, base64_img, mime_type, max_tokens=450)

        if resultado["exito"]:
            resp = resultado["respuesta"]
            # Barcode: extract only EAN-12/13 digits — LLM often appends extra text
            raw_bc = str(resp["codigo_barras"]) if resp.get("codigo_barras") else None
            barcode = None
            if raw_bc:
                import re as _re
                bc_match = _re.search(r'\b(\d{12,13})\b', raw_bc)
                barcode = bc_match.group(1) if bc_match else None

            return {
                "exito": True,
                "modelo": resultado["modelo_usado"],
                "producto": resp.get("producto"),
                "precio_regular": resp.get("precio_regular"),
                "tiene_descuento": bool(resp.get("tiene_descuento", False)),
                "precio_con_descuento": resp.get("precio_con_descuento"),
                "precio_que_se_paga": resp.get("precio_que_se_paga"),
                "unidad": resp.get("unidad"),
                "volumen_ml": resp.get("volumen_ml"),
                "cantidad": resp.get("cantidad"),
                "codigo_barras": barcode,
                "plu": str(resp["plu"]) if resp.get("plu") else None,
                "fecha_promo_inicio": resp.get("fecha_promo_inicio"),
                "fecha_promo_fin": resp.get("fecha_promo_fin"),
                "confianza": float(resp.get("confianza", 0.5)),
            }

        return {"exito": False, "razon": resultado.get("razon", "Fallo de visión")}

    def normalizar_producto(self, texto_raw: str) -> str:
        """Normaliza un nombre de producto extraído por OCR."""
        if not texto_raw or len(texto_raw) < 3:
            return texto_raw or ""

        prompt = (
            f"Nombre de producto OCR (puede tener errores): \"{texto_raw}\"\n"
            f"Devuelve nombre limpio y normalizado en español.\n"
            f'JSON:{{"nombre":"nombre normalizado"}}'
        )

        resultado = self.preguntar_con_rotacion(prompt, max_tokens=60)
        if resultado["exito"]:
            return resultado["respuesta"].get("nombre", texto_raw)
        return texto_raw

    def resumen(self) -> dict:
        return {
            "disponible": self.disponible,
            "llamadas_totales": self.llamadas_totales,
            "errores_totales": self.errores_totales,
        }


# ── Singleton global ──────────────────────────────────────────────────────────
_openrouter_instance: Optional[OpenRouterService] = None


def get_openrouter_service() -> OpenRouterService:
    global _openrouter_instance
    if _openrouter_instance is None:
        _openrouter_instance = OpenRouterService(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            max_models_to_try=int(os.getenv("OPENROUTER_MAX_MODELS", "5")),
        )
    return _openrouter_instance
