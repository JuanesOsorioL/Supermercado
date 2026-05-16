"""
Price extraction and query webservice for supermarket tracking.
Handles image processing, OCR (with LLM Vision fallback), confirmation flow, and price queries.
"""

from fastapi import APIRouter, HTTPException
from fastapi_utils.cbv import cbv
from datetime import datetime
from pathlib import Path
import base64
import uuid
import cv2
import numpy as np

from endpoints.dto.message_dto import (
    PriceExtractRequestDTO,
    PriceExtractResponseDTO,
    ConfirmPriceRequestDTO,
    ConfirmPriceResponseDTO,
    SavePriceRequestDTO,
    ProductQueryRequestDTO,
    ProductQueryResponseDTO,
    ProductPriceInfo,
    PriceComparisonRequestDTO,
    PriceComparisonResponseDTO,
    StorePrice,
)

from utils.database import (
    init_db,
    save_observation,
    confirm_observation,
    get_observation,
    search_products,
    get_cheapest_store,
    compare_prices,
    normalize_product_name,
    get_active_session,
    touch_session,
    catalog_get,
    catalog_upsert,
    get_expired_promos,
    get_all_products,
)

from utils.ocr import extract_price_info
from services.openrouter_service import get_openrouter_service

# ═══════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════

UPLOADS = Path("./uploads")
UPLOADS.mkdir(exist_ok=True)

LLM_VISION_MIN_CONFIDENCE = 0.40  # Lowered: even partial LLM is better than OCR on stylized labels

price_webservice_api_router = APIRouter()


# ═══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def save_base64_image(file_b64: str, mime_type: str) -> Path:
    """Save base64 encoded image to disk."""
    ext = (mime_type.split("/")[-1] or "jpg").lower()
    if ext == "jpeg":
        ext = "jpg"
    fid = str(uuid.uuid4())
    out = UPLOADS / f"{fid}.{ext}"
    out.write_bytes(base64.b64decode(file_b64))
    return out


def load_image(path: Path) -> np.ndarray:
    """Load image from path using OpenCV."""
    return cv2.imread(str(path))


def _safe_int(value) -> int:
    """
    Parse Colombian price to integer COP.
    Handles:
      38650    → 38650   (already integer)
      38.650   → 38650   (dot = thousands separator, 3 decimals)
      38.65    → 38650   (LLM dropped trailing 0 — pad to 3 decimals)
      24.990   → 24990
    """
    if value is None:
        return None
    import re as _r
    s = str(value).strip().replace("$", "").replace(" ", "")
    if not s:
        return None
    try:
        # Colombian notation: digits.2or3digits  (dot is thousands separator)
        m = _r.match(r'^(\d{1,5})[.,](\d{2,3})$', s)
        if m:
            whole, frac = m.group(1), m.group(2)
            padded = frac.ljust(3, '0')   # "65" → "650" | "650" stays "650"
            val = int(whole + padded)
            if 500 <= val <= 1_999_999:
                return val
        # No separator or already plain integer: strip non-digits
        clean = _r.sub(r'[^\d]', '', s)
        return int(clean) if clean else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

def _catalog_shortcut_response(
    req, store, session, img_path, barcode, catalog_hit,
    llm_price: int = None, llm_has_discount: bool = False,
    llm_original_price: int = None, llm_discounted_price: int = None,
    llm_confidence: float = 1.0,
) -> PriceExtractResponseDTO:
    """
    Retorna respuesta inmediata usando el catálogo para el nombre del producto.
    Si llm_price está disponible, se incluye directamente (user solo dice 'si').
    Si no hay precio, se pide al usuario.
    """
    product_raw = catalog_hit["product_name"].upper()
    product_normalized = normalize_product_name(product_raw).upper()
    volume_ml = catalog_hit.get("volume_ml")
    quantity = catalog_hit.get("quantity")

    has_price = llm_price is not None
    confirmation_fields = [] if has_price else ["price"]

    row = {
        "user_id": req.user_id, "store": store,
        "product_raw": product_raw, "product_normalized": product_normalized,
        "price": llm_price, "barcode": barcode,
        "volume_ml": volume_ml, "quantity": quantity,
        "plu": None, "unit_type": None,
        "is_promo": llm_has_discount,
        "has_discount": llm_has_discount,
        "original_price": llm_original_price,
        "discounted_price": llm_discounted_price if llm_has_discount else None,
        "needs_confirmation": not has_price,
        "ocr_confidence": llm_confidence,
        "image_path": str(img_path), "extraction_method": "catalog",
        "created_at": datetime.utcnow().isoformat(),
    }
    if session:
        touch_session(session["id"])
    return PriceExtractResponseDTO(
        store=store, product_raw=product_raw, price=llm_price,
        barcode=barcode, volume_ml=volume_ml, quantity=quantity,
        is_promo=llm_has_discount, has_discount=llm_has_discount,
        original_price=llm_original_price,
        discounted_price=llm_discounted_price if llm_has_discount else None,
        ocr_confidence=llm_confidence, needs_confirmation=not has_price,
        confirmation_fields=confirmation_fields, saved=False,
        observation_id=None,
        extraction_method="catalog", product_source="catalog",
    )


@cbv(price_webservice_api_router)
class PriceWebService:

    @price_webservice_api_router.post(
        "/api/price/extract",
        response_model=PriceExtractResponseDTO,
        summary="Extract price from label image",
        description="Process a supermarket label image. Tries LLM Vision first, falls back to OCR.",
    )
    async def extract(self, req: PriceExtractRequestDTO):
        """
        Extract price information from a supermarket label image.
        Priority: (1) LLM Vision via OpenRouter, (2) Tesseract OCR.
        Includes discount detection (original_price, has_discount, discounted_price).
        """
        try:
            return await self._extract_impl(req)
        except HTTPException:
            raise
        except Exception as e:
            import traceback
            print(f"[EXTRACT ERROR] {type(e).__name__}: {e}")
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    async def _extract_impl(self, req: PriceExtractRequestDTO):
        init_db()

        if not req.user_id:
            raise HTTPException(status_code=400, detail="user_id es requerido")
        if not req.file_base64 or not req.mime_type:
            raise HTTPException(status_code=400, detail="file_base64 y mime_type son requeridos")

        # Save image to disk
        try:
            img_path = save_base64_image(req.file_base64, req.mime_type)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Base64 inválido: {e}")

        # Determine store: explicit in request > active session > "desconocido"
        store = (req.store or "").lower().strip()
        session = None
        if not store or store == "desconocido":
            session = get_active_session(req.user_id)
            if session:
                store = session["store"]
        if not store:
            store = "desconocido"

        # ─────────────────────────────────────────────────────
        # STRATEGY 0: pyzbar → catalog shortcut (free, instant)
        # Si el barcode está en catálogo, no se gasta LLM ni OCR.
        # Solo pedimos el precio al usuario.
        # ─────────────────────────────────────────────────────
        _quick_img = load_image(img_path)
        _pyzbar_barcode = None
        if _quick_img is not None:
            from utils.ocr import read_barcode, crop_roi as _crop
            _pyzbar_barcode = read_barcode(_quick_img) or read_barcode(_crop(_quick_img, "barcode"))
            print(f"[PYZBAR] barcode={_pyzbar_barcode}")
            if _pyzbar_barcode:
                catalog_hit = catalog_get(_pyzbar_barcode, store if store != "desconocido" else None)
                if catalog_hit:
                    print(f"[CATALOG HIT pyzbar] barcode={_pyzbar_barcode} producto={catalog_hit['product_name']}")
                    return _catalog_shortcut_response(
                        req, store, session, img_path, _pyzbar_barcode, catalog_hit
                    )

        # ─────────────────────────────────────────────────────
        # STRATEGY 1: LLM Vision (free via OpenRouter)
        # ─────────────────────────────────────────────────────
        extraction_method = "ocr"
        llm_result = None
        or_service = get_openrouter_service()

        if or_service.disponible:
            try:
                llm_result = or_service.extraer_precio_imagen(req.file_base64, req.mime_type, store=store)
                if llm_result:
                    _sep = "─" * 50
                    print(f"\n[IA] {_sep}")
                    print(f"[IA] modelo       : {llm_result.get('modelo', '?')}")
                    print(f"[IA] exito        : {llm_result.get('exito')}")
                    print(f"[IA] confianza    : {llm_result.get('confianza', '?')}")
                    print(f"[IA] producto     : {llm_result.get('producto')}")
                    print(f"[IA] precio_reg   : {llm_result.get('precio_regular')}")
                    print(f"[IA] tiene_descto : {llm_result.get('tiene_descuento')}")
                    print(f"[IA] precio_dcto  : {llm_result.get('precio_con_descuento')}")
                    print(f"[IA] precio_paga  : {llm_result.get('precio_que_se_paga')}")
                    print(f"[IA] unidad       : {llm_result.get('unidad')}")
                    print(f"[IA] volumen_ml   : {llm_result.get('volumen_ml')}")
                    print(f"[IA] cantidad     : {llm_result.get('cantidad')}")
                    print(f"[IA] barcode      : {llm_result.get('codigo_barras')}")
                    print(f"[IA] plu          : {llm_result.get('plu')}")
                    print(f"[IA] promo_inicio : {llm_result.get('fecha_promo_inicio')}")
                    print(f"[IA] promo_fin    : {llm_result.get('fecha_promo_fin')}")
                    if not llm_result.get('exito'):
                        print(f"[IA] razon        : {llm_result.get('razon')}")
                    print(f"[IA] {_sep}\n")
            except Exception as e:
                print(f"LLM Vision error: {e}")
                llm_result = None

        promo_start_date = None
        promo_end_date = None
        barcode_from_llm: str = None
        barcode_from_ocr: str = _pyzbar_barcode  # best hardware-scan barcode

        # ─────────────────────────────────────────────────────
        # Build result from LLM or fall back to OCR
        # ─────────────────────────────────────────────────────
        if llm_result and llm_result.get("exito") and llm_result.get("confianza", 0) >= LLM_VISION_MIN_CONFIDENCE:
            extraction_method = "llm_vision"

            product_raw = llm_result.get("producto")
            product_normalized = normalize_product_name(product_raw) if product_raw else None

            precio_regular = _safe_int(llm_result.get("precio_regular"))
            tiene_descuento = bool(llm_result.get("tiene_descuento"))
            precio_con_descuento = _safe_int(llm_result.get("precio_con_descuento"))
            precio_que_se_paga = _safe_int(llm_result.get("precio_que_se_paga"))

            price = precio_que_se_paga or (precio_con_descuento if tiene_descuento else precio_regular)
            original_price = precio_regular if tiene_descuento else None

            unit_type = llm_result.get("unidad")
            volume_ml = _safe_int(llm_result.get("volumen_ml"))
            quantity = _safe_int(llm_result.get("cantidad"))
            barcode = llm_result.get("codigo_barras")
            plu = llm_result.get("plu")
            confidence = llm_result.get("confianza", 0.9)

            # ── Catalog shortcut via LLM barcode (pyzbar falló pero LLM leyó el código) ──
            if barcode and not _pyzbar_barcode:
                catalog_hit = catalog_get(barcode, store if store != "desconocido" else None)
                if catalog_hit:
                    print(f"[CATALOG HIT llm] barcode={barcode} producto={catalog_hit['product_name']} precio={price}")
                    return _catalog_shortcut_response(
                        req, store, session, img_path, barcode, catalog_hit,
                        llm_price=price,
                        llm_has_discount=tiene_descuento,
                        llm_original_price=original_price,
                        llm_discounted_price=precio_con_descuento,
                        llm_confidence=confidence,
                    )

            # LLM promo dates (may be None if model didn't detect them)
            promo_start_date = llm_result.get("fecha_promo_inicio")
            promo_end_date   = llm_result.get("fecha_promo_fin")

            # Capture LLM barcode BEFORE ocr_fill may add another value
            barcode_from_llm = barcode

            # Run OCR to fill gaps LLM may have missed (barcode, volume, PLU, price, promo dates)
            _img = load_image(img_path)
            if _img is not None:
                ocr_fill = extract_price_info(_img)
                print(f"[OCR-fill] precio={ocr_fill.price} barcode={ocr_fill.barcode} "
                      f"vol={ocr_fill.volume_ml} promo_fin={ocr_fill.promo_end_date}")
                if ocr_fill.barcode and not barcode_from_ocr:
                    barcode_from_ocr = ocr_fill.barcode
                if not volume_ml:
                    volume_ml = ocr_fill.volume_ml
                if not plu:
                    plu = ocr_fill.plu
                if not price and ocr_fill.price:
                    price = ocr_fill.price
                # OCR promo dates override LLM only if LLM missed them
                if not promo_start_date:
                    promo_start_date = ocr_fill.promo_start_date
                if not promo_end_date:
                    promo_end_date = ocr_fill.promo_end_date

            # Uppercase product names
            if product_raw:
                product_raw = product_raw.upper()
            if product_normalized:
                product_normalized = product_normalized.upper()

            confirmation_fields = []
            if not product_raw:
                confirmation_fields.append("product")
            if not price:
                confirmation_fields.append("price")
            needs_confirmation = len(confirmation_fields) > 0

        else:
            # OCR fallback (LLM failed or confidence too low)
            img = load_image(img_path)
            if img is None:
                raise HTTPException(status_code=400, detail="No se pudo leer la imagen")

            ocr_result = extract_price_info(img)
            _sep = "─" * 50
            print(f"\n[OCR] {_sep}")
            print(f"[OCR] producto    : {ocr_result.product_raw!r}")
            print(f"[OCR] precio      : {ocr_result.price}")
            print(f"[OCR] has_discount: {ocr_result.has_discount}")
            print(f"[OCR] orig_price  : {ocr_result.original_price}")
            print(f"[OCR] disc_price  : {ocr_result.discounted_price}")
            print(f"[OCR] barcode     : {ocr_result.barcode}")
            print(f"[OCR] volume_ml   : {ocr_result.volume_ml}")
            print(f"[OCR] plu         : {ocr_result.plu}")
            print(f"[OCR] promo_inicio: {ocr_result.promo_start_date}")
            print(f"[OCR] promo_fin   : {ocr_result.promo_end_date}")
            print(f"[OCR] confianza   : {ocr_result.confidence:.2f}")
            print(f"[OCR] {_sep}\n")

            product_raw = ocr_result.product_raw
            product_normalized = ocr_result.product_normalized
            price = ocr_result.price
            unit_type = ocr_result.unit_type
            volume_ml = ocr_result.volume_ml
            quantity = ocr_result.quantity
            barcode = ocr_result.barcode
            plu = ocr_result.plu
            confidence = ocr_result.confidence
            needs_confirmation = ocr_result.needs_confirmation
            confirmation_fields = ocr_result.confirmation_fields

            # OCR barcode (pyzbar already in barcode_from_ocr; add ocr_result if pyzbar missed it)
            if ocr_result.barcode and not barcode_from_ocr:
                barcode_from_ocr = ocr_result.barcode

            # OCR discount detection (from enhanced regex in ocr.py)
            tiene_descuento = getattr(ocr_result, "has_discount", False)
            original_price = getattr(ocr_result, "original_price", None)
            precio_con_descuento = getattr(ocr_result, "discounted_price", None)

            # Promo dates from OCR
            promo_start_date = getattr(ocr_result, "promo_start_date", None)
            promo_end_date = getattr(ocr_result, "promo_end_date", None)

            # Uppercase product names
            if product_raw:
                product_raw = product_raw.upper()
            if product_normalized:
                product_normalized = product_normalized.upper()

        # ─────────────────────────────────────────────────────
        # BARCODE FALLBACK — always try all methods if OCR still hasn't found one
        # ─────────────────────────────────────────────────────
        if not barcode_from_ocr:
            try:
                from utils.ocr import read_barcode, extract_barcode_from_bottom, parse_barcode_from_text, run_ocr, preprocess_image as _preimg
                _img = load_image(img_path)
                if _img is not None:
                    _found = read_barcode(_img) or extract_barcode_from_bottom(_img)
                    if not _found:
                        _text, _ = run_ocr(_preimg(_img, "text"))
                        _found = parse_barcode_from_text(_text)
                    if _found:
                        barcode_from_ocr = _found
                        print(f"[BARCODE-FALLBACK] encontrado={_found}")
            except Exception as _e:
                print(f"Barcode fallback error: {_e}")

        # ─────────────────────────────────────────────────────
        # BARCODE CONFLICT RESOLUTION
        # ─────────────────────────────────────────────────────
        barcode_conflict = False
        if barcode_from_llm and barcode_from_ocr and barcode_from_llm != barcode_from_ocr:
            barcode_conflict = True
            barcode = None  # frontend will ask the user to pick
            print(f"[BARCODE CONFLICT] IA={barcode_from_llm} OCR={barcode_from_ocr}")
        elif barcode_from_llm:
            barcode = barcode_from_llm
        elif barcode_from_ocr:
            barcode = barcode_from_ocr
        # else barcode stays None

        # ─────────────────────────────────────────────────────
        # CATALOG LOOKUP — barcode is reliable; product name is not
        # ─────────────────────────────────────────────────────
        product_source = extraction_method  # "llm_vision" | "ocr"
        if barcode:
            catalog_entry = catalog_get(barcode, store if store != "desconocido" else None)
            if catalog_entry:
                product_raw = catalog_entry["product_name"]
                product_normalized = normalize_product_name(product_raw)
                if catalog_entry.get("volume_ml") and not volume_ml:
                    volume_ml = catalog_entry["volume_ml"]
                if catalog_entry.get("quantity") and not quantity:
                    quantity = catalog_entry["quantity"]
                product_source = "catalog"
                # Catalog name is trusted — remove product from confirmation fields
                if "product" in confirmation_fields:
                    confirmation_fields = [f for f in confirmation_fields if f != "product"]
                needs_confirmation = len(confirmation_fields) > 0

        # ─────────────────────────────────────────────────────
        # Save observation
        # ─────────────────────────────────────────────────────
        row = {
            "user_id": req.user_id,
            "store": store,
            "product_raw": product_raw,
            "product_normalized": product_normalized,
            "price": price,
            "unit_type": unit_type,
            "volume_ml": volume_ml,
            "quantity": quantity,
            "barcode": barcode,
            "plu": plu,
            "is_promo": tiene_descuento,
            "needs_confirmation": needs_confirmation,
            "ocr_confidence": confidence,
            "image_path": str(img_path),
            "original_price": original_price,
            "has_discount": tiene_descuento,
            "discounted_price": precio_con_descuento if tiene_descuento else None,
            "extraction_method": extraction_method,
            "created_at": datetime.utcnow().isoformat(),
            "promo_start_date": promo_start_date,
            "promo_end_date": promo_end_date,
        }
        product_source_final = product_source

        print(f"[EXTRACT] producto={product_raw!r} precio={price} descuento={tiene_descuento} "
              f"precio_dcto={precio_con_descuento} promo_fin={promo_end_date} barcode={barcode}")

        # Keep session alive
        observation_id = None
        saved = False
        if session:
            touch_session(session["id"])

        return PriceExtractResponseDTO(
            store=store,
            product_raw=product_raw,
            price=price,
            unit_type=unit_type,
            volume_ml=volume_ml,
            quantity=quantity,
            barcode=barcode,
            plu=plu,
            is_promo=tiene_descuento,
            original_price=original_price,
            has_discount=tiene_descuento,
            discounted_price=precio_con_descuento if tiene_descuento else None,
            ocr_confidence=confidence,
            needs_confirmation=needs_confirmation,
            confirmation_fields=confirmation_fields,
            saved=saved,
            observation_id=observation_id,
            extraction_method=extraction_method,
            product_source=product_source_final,
            promo_start_date=promo_start_date,
            promo_end_date=promo_end_date,
            barcode_llm=barcode_from_llm,
            barcode_ocr=barcode_from_ocr,
            barcode_conflict=barcode_conflict,
        )

    @price_webservice_api_router.post(
        "/api/price/confirm",
        response_model=ConfirmPriceResponseDTO,
        summary="Confirm extracted price data",
    )
    async def confirm(self, req: ConfirmPriceRequestDTO):
        """Confirm or correct extracted price data including discount fields."""
        obs = get_observation(req.observation_id)
        if not obs:
            raise HTTPException(status_code=404, detail="Observación no encontrada")

        if obs["user_id"] != req.user_id:
            raise HTTPException(status_code=403, detail="No autorizado")

        confirmed_data = {}
        if req.confirmed_price is not None:
            confirmed_data["price"] = req.confirmed_price
        if req.confirmed_product:
            confirmed_data["product"] = req.confirmed_product.upper()
        if req.confirmed_store:
            confirmed_data["store"] = req.confirmed_store
        if req.confirmed_barcode:
            confirmed_data["barcode"] = req.confirmed_barcode
        if req.confirmed_has_discount is not None:
            confirmed_data["has_discount"] = req.confirmed_has_discount
        if req.confirmed_discounted_price is not None:
            confirmed_data["discounted_price"] = req.confirmed_discounted_price
        if req.confirmed_volume_ml is not None:
            confirmed_data["volume_ml"] = req.confirmed_volume_ml
        if req.confirmed_promo_end_date is not None:
            confirmed_data["promo_end_date"] = req.confirmed_promo_end_date

        success = confirm_observation(req.observation_id, confirmed_data)

        # Save to catalog on ANY confirmation
        # Use barcode from observation OR from the confirmation request (passed by frontend)
        barcode_for_catalog = obs.get("barcode") or req.confirmed_barcode
        if success and barcode_for_catalog:
            product_to_save = req.confirmed_product or obs.get("product_raw")
            store_for_catalog = (req.confirmed_store or obs.get("store") or "")
            if store_for_catalog == "desconocido":
                store_for_catalog = ""
            if product_to_save:
                try:
                    catalog_upsert(
                        barcode=barcode_for_catalog,
                        product_name=product_to_save,
                        store=store_for_catalog or None,
                        updated_by=req.user_id,
                    )
                except Exception as e:
                    print(f"catalog_upsert error: {e}")

        if success:
            return ConfirmPriceResponseDTO(success=True, message="Datos confirmados y guardados correctamente")
        return ConfirmPriceResponseDTO(success=False, message="No se pudo actualizar la observación")

    @price_webservice_api_router.post(
        "/api/price/save",
        summary="Guardar producto confirmado por el usuario",
    )
    async def save_confirmed(self, req: SavePriceRequestDTO):
        """
        Guarda una nueva observación de precio solo cuando el usuario confirma
        explícitamente desde el menú. No se llama durante el extract.
        """
        product_raw = (req.product_raw or "").upper().strip()
        product_normalized = normalize_product_name(product_raw).upper()

        row = {
            "user_id": req.user_id,
            "store": req.store,
            "product_raw": product_raw,
            "product_normalized": product_normalized,
            "price": req.price,
            "has_discount": req.has_discount,
            "original_price": req.original_price,
            "discounted_price": req.discounted_price if req.has_discount else None,
            "barcode": req.barcode,
            "volume_ml": req.volume_ml,
            "promo_end_date": req.promo_end_date,
            "needs_confirmation": False,
            "ocr_confidence": req.ocr_confidence or 1.0,
            "extraction_method": req.extraction_method or "confirmed",
            "created_at": datetime.utcnow().isoformat(),
        }

        observation_id = save_observation(row)
        # Mark as confirmed immediately (user explicitly approved)
        confirm_observation(observation_id, {})

        # Update product catalog if barcode present
        if req.barcode and product_raw:
            try:
                catalog_upsert(
                    barcode=req.barcode,
                    product_name=product_raw,
                    store=req.store or None,
                    updated_by=req.user_id,
                )
            except Exception as e:
                print(f"[save_confirmed] catalog_upsert error: {e}")

        print(f"[SAVED] {product_raw!r} precio={req.price} tienda={req.store} barcode={req.barcode}")
        return {"success": True, "observation_id": observation_id}

    @price_webservice_api_router.post(
        "/api/price/search",
        response_model=ProductQueryResponseDTO,
        summary="Search products and prices",
    )
    async def search(self, req: ProductQueryRequestDTO):
        """Search for products and their prices across stores."""
        init_db()

        results = search_products(query=req.query, store=req.store, limit=req.limit)

        if not results:
            return ProductQueryResponseDTO(
                query=req.query,
                results=[],
                message=f"No encontré productos que coincidan con '{req.query}'",
            )

        product_infos = []
        for r in results:
            product_infos.append(ProductPriceInfo(
                product_name=r.get("product_raw") or "Desconocido",
                store=r.get("store") or "Desconocido",
                price=r.get("price") or 0,
                unit_price=r.get("unit_price"),
                unit_type=r.get("unit_type"),
                is_promo=bool(r.get("is_promo")),
                has_discount=bool(r.get("has_discount")),
                original_price=r.get("original_price"),
                discounted_price=r.get("discounted_price"),
                last_seen=datetime.fromisoformat(r["created_at"]) if r.get("created_at") else datetime.utcnow(),
                barcode=r.get("barcode"),
            ))

        effective = lambda x: x.discounted_price if x.has_discount and x.discounted_price else x.price
        cheapest = min(product_infos, key=effective) if product_infos else None

        return ProductQueryResponseDTO(
            query=req.query,
            results=product_infos,
            cheapest_store=cheapest.store if cheapest else None,
            cheapest_price=effective(cheapest) if cheapest else None,
            message=f"Encontré {len(product_infos)} resultados para '{req.query}'",
        )

    @price_webservice_api_router.post(
        "/api/price/compare",
        response_model=PriceComparisonResponseDTO,
        summary="Compare prices across stores",
    )
    async def compare(self, req: PriceComparisonRequestDTO):
        """Compare prices of a specific product across all stores."""
        init_db()

        if not req.barcode and not req.product_name:
            raise HTTPException(status_code=400, detail="Debes proporcionar barcode o product_name")

        results = compare_prices(barcode=req.barcode, product_name=req.product_name)

        if not results:
            return PriceComparisonResponseDTO(
                product_name=req.product_name or "Producto no encontrado",
                barcode=req.barcode,
                stores=[],
            )

        store_prices = []
        for r in results:
            store_prices.append(StorePrice(
                store=r.get("store") or "Desconocido",
                price=r.get("price") or 0,
                unit_price=r.get("unit_price"),
                is_promo=bool(r.get("is_promo")),
                has_discount=bool(r.get("has_discount")),
                original_price=r.get("original_price"),
                discounted_price=r.get("discounted_price"),
                last_updated=datetime.fromisoformat(r["last_updated"]) if r.get("last_updated") else datetime.utcnow(),
            ))

        effective = lambda x: x.discounted_price if x.has_discount and x.discounted_price else x.price
        cheapest = min(store_prices, key=effective) if store_prices else None
        most_expensive = max(store_prices, key=effective) if store_prices else None

        savings = None
        if cheapest and most_expensive and len(store_prices) > 1:
            savings = effective(most_expensive) - effective(cheapest)

        product_name = results[0].get("product_raw") or req.product_name or "Producto"

        return PriceComparisonResponseDTO(
            product_name=product_name,
            barcode=req.barcode,
            stores=store_prices,
            cheapest=cheapest,
            most_expensive=most_expensive,
            savings=savings,
        )

    @price_webservice_api_router.get(
        "/api/price/cheapest",
        summary="Find cheapest store for a product",
    )
    async def cheapest(self, barcode: str = None, product: str = None):
        """Quick lookup to find the cheapest store for a product."""
        init_db()

        if not barcode and not product:
            raise HTTPException(status_code=400, detail="Debes proporcionar barcode o product")

        result = get_cheapest_store(barcode=barcode, product_name=product)

        if not result:
            return {"found": False, "message": "Producto no encontrado en la base de datos"}

        return {
            "found": True,
            "product": result.get("product_raw"),
            "store": result.get("store"),
            "price": result.get("price"),
            "has_discount": bool(result.get("has_discount")),
            "original_price": result.get("original_price"),
            "discounted_price": result.get("discounted_price"),
            "effective_price": result.get("effective_price", result.get("price")),
            "is_promo": bool(result.get("is_promo")),
            "barcode": result.get("barcode"),
        }

    @price_webservice_api_router.get(
        "/api/price/expired-promos",
        summary="Get observations with expired promo dates",
    )
    async def expired_promos(self):
        """Return all observations where has_discount=1 and promo_end_date has passed."""
        rows = get_expired_promos()
        return {"expired": rows, "count": len(rows)}

    @price_webservice_api_router.get(
        "/api/price/all-products",
        summary="Get all products for a user grouped by store",
    )
    async def all_products(self, user_id: str):
        """Return every product recorded by user, grouped by store."""
        grouped = get_all_products(user_id)
        return {"stores": grouped}

    # ─── ADMIN ────────────────────────────────────────────────────────
    @price_webservice_api_router.delete(
        "/api/admin/clear",
        summary="Clear all data (testing only)",
    )
    async def clear_all(self, user_id: str = None):
        """Delete all observations, catalog, sessions, and shopping lists."""
        from utils.database import get_db
        with get_db() as con:
            cur = con.cursor()
            cur.execute("DELETE FROM price_observations")
            cur.execute("DELETE FROM product_catalog")
            cur.execute("DELETE FROM scan_sessions")
            cur.execute("DELETE FROM user_shopping_lists")
            cur.execute("DELETE FROM price_history")
            con.commit()
            counts = {
                "observations": cur.execute("SELECT changes()").fetchone()[0],
            }
        return {"cleared": True, "message": "Todos los registros eliminados"}

    @price_webservice_api_router.get(
        "/api/admin/catalog",
        summary="Show product catalog contents with latest prices",
    )
    async def show_catalog(self):
        """List all entries in the product catalog with the latest observed price."""
        from utils.database import get_db
        with get_db() as con:
            cur = con.cursor()
            cur.execute("""
                SELECT
                    c.barcode,
                    c.store,
                    c.product_name,
                    c.volume_ml,
                    c.updated_at,
                    o.price,
                    o.has_discount,
                    o.original_price,
                    o.discounted_price,
                    o.created_at AS last_seen
                FROM product_catalog c
                LEFT JOIN (
                    SELECT barcode, store, price, has_discount, original_price,
                           discounted_price, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY barcode, COALESCE(store,'')
                               ORDER BY created_at DESC
                           ) AS rn
                    FROM price_observations
                    WHERE price IS NOT NULL
                ) o ON o.barcode = c.barcode
                    AND COALESCE(o.store,'') = COALESCE(c.store,'')
                    AND o.rn = 1
                ORDER BY c.updated_at DESC
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return {"count": len(rows), "catalog": rows}
