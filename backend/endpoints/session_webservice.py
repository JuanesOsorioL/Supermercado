"""
Session management webservice.
Handles store visit sessions: start, update products by number, end.
"""

from fastapi import APIRouter, HTTPException
from datetime import datetime
import datetime as _dt

from endpoints.dto.message_dto import (
    SessionStartRequestDTO,
    SessionStartResponseDTO,
    SessionProductDTO,
    SessionUpdateProductRequestDTO,
    SessionEndRequestDTO,
    SessionEndResponseDTO,
)
from utils.database import (
    init_db,
    start_session,
    get_active_session,
    end_session,
    touch_session,
    increment_session_updated,
    get_store_products,
    update_observation,
    get_observation,
)

session_api_router = APIRouter()


@session_api_router.post(
    "/api/session/start",
    response_model=SessionStartResponseDTO,
    summary="Iniciar sesión de escaneo en una tienda",
)
async def session_start(req: SessionStartRequestDTO):
    """
    Inicia una sesión de visita a un supermercado.
    Retorna la lista numerada de productos ya registrados en esa tienda.
    """
    init_db()

    store = req.store.lower().strip()
    session_id = start_session(req.user_id, store)

    # Get existing products for this store
    raw_products = get_store_products(req.user_id, store)

    products = []
    for i, p in enumerate(raw_products, start=1):
        created_at = p.get("created_at", "")
        try:
            days_ago = (datetime.utcnow() - datetime.fromisoformat(created_at)).days
        except Exception:
            days_ago = 0

        products.append(SessionProductDTO(
            observation_id=p["id"],
            position=i,
            product_normalized=p.get("product_normalized", ""),
            product_raw=p.get("product_raw"),
            price=p.get("price"),
            has_discount=bool(p.get("has_discount")),
            original_price=p.get("original_price"),
            discounted_price=p.get("discounted_price"),
            days_ago=days_ago,
            promo_end_date=p.get("promo_end_date"),
            barcode=p.get("barcode"),
            volume_ml=p.get("volume_ml"),
        ))

    msg = f"Sesión iniciada en {store.capitalize()}."
    if products:
        msg += f" Tienes {len(products)} producto(s) registrado(s)."
    else:
        msg += " Aún no tienes productos de esta tienda. ¡Manda fotos!"

    return SessionStartResponseDTO(
        session_id=session_id,
        store=store,
        products=products,
        message=msg,
    )


@session_api_router.get(
    "/api/session/active",
    summary="Obtener sesión activa del usuario",
)
async def session_get_active(user_id: str):
    """Retorna la sesión activa del usuario, si existe."""
    session = get_active_session(user_id)
    if not session:
        return {"active": False}

    raw_products = get_store_products(user_id, session["store"])
    products = []
    for i, p in enumerate(raw_products, start=1):
        try:
            days_ago = (datetime.utcnow() - datetime.fromisoformat(p.get("created_at", ""))).days
        except Exception:
            days_ago = 0

        products.append({
            "observation_id": p["id"],
            "position": i,
            "product_normalized": p.get("product_normalized", ""),
            "product_raw": p.get("product_raw"),
            "price": p.get("price"),
            "has_discount": bool(p.get("has_discount")),
            "original_price": p.get("original_price"),
            "discounted_price": p.get("discounted_price"),
            "days_ago": days_ago,
        })

    return {
        "active": True,
        "session_id": session["id"],
        "store": session["store"],
        "started_at": session["started_at"],
        "products": products,
    }


@session_api_router.post(
    "/api/session/update-product",
    summary="Actualizar precio de un producto en la sesión activa",
)
async def session_update_product(req: SessionUpdateProductRequestDTO):
    """
    Actualiza el precio (y descuento) de un producto durante una sesión.
    Soporta actualización por número de posición o por observation_id.
    """
    obs = get_observation(req.observation_id)
    if not obs:
        raise HTTPException(status_code=404, detail="Producto no encontrado")

    updates = {}
    now_iso = datetime.utcnow().isoformat()

    if req.price is not None:
        # If product had discount before and now has regular price, save as original_price
        if obs.get("has_discount") and req.has_discount is False:
            updates["original_price"] = None
            updates["has_discount"] = 0
            updates["discounted_price"] = None
            updates["discount_verified_at"] = now_iso
        else:
            updates["price"] = req.price

    if req.has_discount is not None:
        updates["has_discount"] = 1 if req.has_discount else 0
        updates["discount_verified_at"] = now_iso

        if req.has_discount and req.price is not None:
            # Price provided is the regular price; discounted_price is separate
            updates["original_price"] = req.price
            if req.discounted_price is not None:
                updates["discounted_price"] = req.discounted_price
                updates["price"] = req.discounted_price  # effective price = discounted
        elif not req.has_discount:
            updates["discounted_price"] = None
            if req.price is not None:
                updates["price"] = req.price

    if not updates:
        return {"success": False, "message": "Nada que actualizar"}

    updates["is_confirmed"] = 1
    updates["needs_confirmation"] = 0

    success = update_observation(req.observation_id, updates)

    if success:
        # Update session activity
        session = get_active_session(req.user_id)
        if session:
            increment_session_updated(session["id"])

    return {
        "success": success,
        "observation_id": req.observation_id,
        "updates": {k: v for k, v in updates.items() if k not in ("is_confirmed", "needs_confirmation")},
    }


@session_api_router.post(
    "/api/session/end",
    response_model=SessionEndResponseDTO,
    summary="Terminar sesión de escaneo",
)
async def session_end_endpoint(req: SessionEndRequestDTO):
    """Cierra la sesión activa y retorna un resumen con productos de descuento a revisar."""
    from utils.database import get_db
    with get_db() as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM scan_sessions WHERE id = ? AND user_id = ?", (req.session_id, req.user_id))
        row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    session = dict(row)
    store = session["store"]
    updated_count = session.get("updated_count", 0)

    raw_products = get_store_products(req.user_id, store)

    today = _dt.date.today()
    expired_products = []
    pos = 1
    for p in raw_products:
        if not p.get("has_discount"):
            continue
        end_date_str = p.get("promo_end_date")
        if end_date_str:
            try:
                end_date = _dt.datetime.strptime(end_date_str, "%d/%m/%Y").date()
                if end_date >= today:
                    continue  # promo still valid, skip
            except ValueError:
                pass  # invalid date → include in review

        try:
            created_at = p.get("created_at", "")
            days_ago = (datetime.utcnow() - datetime.fromisoformat(created_at)).days
        except Exception:
            days_ago = 0

        expired_products.append(SessionProductDTO(
            observation_id=p["id"],
            position=pos,
            product_normalized=(p.get("product_normalized") or "").upper(),
            product_raw=(p.get("product_raw") or "").upper(),
            price=p.get("price"),
            has_discount=True,
            original_price=p.get("original_price"),
            discounted_price=p.get("discounted_price"),
            days_ago=days_ago,
            promo_end_date=end_date_str,
            barcode=p.get("barcode"),
            volume_ml=p.get("volume_ml"),
        ))
        pos += 1

    end_session(req.session_id)

    return SessionEndResponseDTO(
        success=True,
        updated_count=updated_count,
        total_count=len(raw_products),
        unverified=[],
        expired_products=expired_products,
    )
