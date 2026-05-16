"""
Supermarket Price Tracker API
FastAPI backend for tracking supermarket prices via WhatsApp images.
"""

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from endpoints.price_webservice import price_webservice_api_router
from endpoints.session_webservice import session_api_router
from endpoints.shopping_webservice import shopping_api_router
from utils.database import init_db
from services.openrouter_service import get_openrouter_service

# ═══════════════════════════════════════════════════════════════
# APP CONFIGURATION
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="Supermarket Price Tracker",
    description="""
    API para rastrear y comparar precios de supermercados.

    ## Funcionalidades

    * **Extracción de precios**: LLM Vision (OpenRouter) + OCR fallback
    * **Descuentos**: Detecta precio original, descuento y precio final
    * **Sesiones de visita**: Inicia sesión en una tienda y actualiza precios por número
    * **Lista de compras**: Guarda tus productos habituales y obtén recomendaciones
    * **Comparación**: Compara precios entre tiendas considerando descuentos
    """,
    version="3.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════
# ROUTERS
# ═══════════════════════════════════════════════════════════════

app.include_router(price_webservice_api_router, tags=["Prices"])
app.include_router(session_api_router, tags=["Sessions"])
app.include_router(shopping_api_router, tags=["Shopping"])


# ═══════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup_event():
    print("Starting Supermarket Price Tracker API v3...")
    init_db()
    print("Database initialized")

    or_service = get_openrouter_service()
    if or_service.disponible:
        print("OpenRouter disponible — LLM Vision activo")
    else:
        print("OpenRouter no configurado — usando solo OCR")


@app.on_event("shutdown")
async def shutdown_event():
    print("Shutting down...")


# ═══════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════

@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "Supermarket Price Tracker", "version": "3.0.0"}


@app.get("/health", tags=["Health"])
async def health():
    from services.groq_service import get_groq_service
    or_service = get_openrouter_service()
    groq_service = get_groq_service()
    return {
        "status": "healthy",
        "database": "ok",
        "ocr": "ok",
        "llm_vision": "ok" if (groq_service.disponible or or_service.disponible) else "no configurado",
        "groq": groq_service.resumen(),
        "openrouter": or_service.resumen(),
    }


@app.get("/test/pyzbar", tags=["Debug"])
async def test_pyzbar():
    """Verifica si pyzbar y libzbar están correctamente instalados."""
    from utils.ocr import zbar_decode
    import numpy as np
    import cv2, base64

    if zbar_decode is None:
        return {
            "pyzbar": "NO DISPONIBLE",
            "causa": "libzbar DLL no encontrado. En Windows: descarga libzbar-64.dll y ponla en C:\\Windows\\System32 o junto al ejecutable de Python.",
            "descarga": "https://github.com/NaturalHistoryMuseum/pyzbar#installation"
        }

    # Crear imagen con un barcode EAN-13 conocido para probar decodificación
    # Usamos una imagen sencilla — si pyzbar carga bien, esto confirma el DLL
    try:
        import barcode as _bc
        from barcode.writer import ImageWriter
        from io import BytesIO
        ean = _bc.get("ean13", "7702177022764", writer=ImageWriter())
        buf = BytesIO()
        ean.write(buf)
        buf.seek(0)
        img_array = np.frombuffer(buf.read(), np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        codes = zbar_decode(img)
        decoded = [c.data.decode() for c in codes]
        return {
            "pyzbar": "OK",
            "dll": "cargado correctamente",
            "test_barcode": "7702177022764",
            "decoded": decoded,
            "resultado": "✅ pyzbar lee barcodes correctamente" if decoded else "⚠️ pyzbar cargó pero no decodificó la imagen de prueba"
        }
    except ImportError:
        # python-barcode not installed — just confirm pyzbar loads
        return {
            "pyzbar": "OK",
            "dll": "cargado correctamente",
            "nota": "Instala 'python-barcode[images]' para el test completo. pyzbar está disponible."
        }
    except Exception as e:
        return {"pyzbar": "OK (DLL cargado)", "test_error": str(e)}


@app.get("/test/llm-vision", tags=["Debug"])
async def test_llm_vision():
    """
    Verifica qué modelos soportan visión enviando una imagen real a cada uno.
    Separa modelos de visión (para imágenes) de modelos de texto (para chat).
    """
    from services.openrouter_service import VISION_FREE_MODELS, PREFERRED_FREE_MODELS
    import requests as req_lib, base64, numpy as np, cv2

    or_service = get_openrouter_service()
    if not or_service.disponible:
        return {"error": "OPENROUTER_API_KEY no configurada"}

    # Imagen de prueba: etiqueta simple con precio
    img = np.ones((120, 320, 3), dtype=np.uint8) * 255
    cv2.putText(img, "$43.100", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
    cv2.putText(img, "SIXPACK ALQUERIA", (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
    _, buf = cv2.imencode(".jpg", img)
    b64 = base64.b64encode(buf).decode()

    prompt = 'Esta imagen tiene un precio. Responde SOLO este JSON: {"precio": 43100, "soporta_vision": true}'

    # Forzar refresh del caché
    import services.openrouter_service as _ors
    _ors._models_cache = []
    _ors._models_cache_ts = 0
    available = set(or_service._get_available_models())

    results = []
    for model_id in VISION_FREE_MODELS:
        try:
            resp = req_lib.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {or_service.api_key}", "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "max_tokens": 60,
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": prompt}
                    ]}]
                },
                timeout=20,
            )
            if resp.status_code == 200:
                content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
                results.append({"model": model_id, "status": "✅ SOPORTA VISIÓN", "respuesta": (content or "")[:100]})
            elif resp.status_code == 404:
                results.append({"model": model_id, "status": "❌ NO SOPORTA IMÁGENES (404)", "detalle": resp.text[:120]})
            elif resp.status_code == 429:
                results.append({"model": model_id, "status": "⚠️ RATE LIMIT (429) — probablemente soporta visión", "detalle": "Intentar más tarde"})
            else:
                results.append({"model": model_id, "status": f"⚠️ HTTP {resp.status_code}", "detalle": resp.text[:120]})
        except Exception as e:
            results.append({"model": model_id, "status": f"❌ ERROR", "detalle": str(e)})

    vision_ok    = [r["model"] for r in results if "✅" in r["status"]]
    vision_maybe = [r["model"] for r in results if "429" in r["status"]]
    vision_no    = [r["model"] for r in results if "❌" in r["status"] and "404" in r["status"]]

    return {
        "resultados": results,
        "confirmados_vision": vision_ok,
        "probablemente_vision": vision_maybe,
        "NO_soportan_imagenes": vision_no,
        "recomendacion": f"VISION_FREE_MODELS = {vision_ok + vision_maybe}"
    }


# ═══════════════════════════════════════════════════════════════
# RUN SERVER
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
