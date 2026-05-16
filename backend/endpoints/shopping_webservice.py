"""
Shopping list and recommendation webservice.
Manages user shopping lists and generates optimized store recommendations.
"""

from fastapi import APIRouter, HTTPException
from collections import defaultdict

from endpoints.dto.message_dto import (
    ShoppingListAddRequestDTO,
    ShoppingListRemoveRequestDTO,
    ShoppingRecommendationItemDTO,
    ShoppingRecommendationResponseDTO,
)
from utils.database import (
    init_db,
    shopping_list_add,
    shopping_list_remove,
    shopping_list_clear,
    shopping_list_get,
    get_shopping_recommendations,
)

shopping_api_router = APIRouter()


@shopping_api_router.post(
    "/api/shopping/list/add",
    summary="Agregar productos a la lista de compras",
)
async def list_add(req: ShoppingListAddRequestDTO):
    """Agrega uno o más productos a la lista personal del usuario."""
    init_db()

    if not req.products:
        raise HTTPException(status_code=400, detail="La lista de productos está vacía")

    shopping_list_add(req.user_id, req.products)

    return {
        "success": True,
        "added": req.products,
        "message": f"Agregué {len(req.products)} producto(s) a tu lista.",
    }


@shopping_api_router.post(
    "/api/shopping/list/remove",
    summary="Quitar un producto de la lista de compras",
)
async def list_remove(req: ShoppingListRemoveRequestDTO):
    """Elimina un producto de la lista personal del usuario."""
    shopping_list_remove(req.user_id, req.product)
    return {"success": True, "message": f"Eliminé '{req.product}' de tu lista."}


@shopping_api_router.post(
    "/api/shopping/list/clear",
    summary="Limpiar toda la lista de compras",
)
async def list_clear(req: dict):
    """Elimina todos los productos de la lista del usuario."""
    shopping_list_clear(req["user_id"])
    return {"success": True, "message": "Lista de compras limpiada."}


@shopping_api_router.get(
    "/api/shopping/list",
    summary="Ver lista de compras del usuario",
)
async def list_get(user_id: str):
    """Retorna la lista de compras del usuario."""
    init_db()
    products = shopping_list_get(user_id)
    return {
        "user_id": user_id,
        "products": products,
        "count": len(products),
    }


@shopping_api_router.get(
    "/api/shopping/recommend",
    response_model=ShoppingRecommendationResponseDTO,
    summary="Recomendación de tienda para lista de compras",
)
async def recommend(user_id: str):
    """
    Para cada producto en la lista del usuario, recomienda la tienda más barata.
    Agrupa por tienda para minimizar viajes.
    """
    init_db()

    raw = get_shopping_recommendations(user_id)

    if not raw:
        return ShoppingRecommendationResponseDTO(
            items=[],
            not_found=[],
            grouped_by_store={},
            total_estimated=0,
        )

    items = []
    not_found = []
    grouped: dict = defaultdict(list)
    total = 0

    for r in raw:
        if r.get("not_found") or not r.get("store"):
            not_found.append(r["product"])
            continue

        item = ShoppingRecommendationItemDTO(
            product=r["product"],
            store=r["store"],
            price=r["price"],
            has_discount=r.get("has_discount", False),
            discounted_price=r.get("discounted_price"),
            effective_price=r.get("effective_price", r["price"]),
            discount_unverified=r.get("discount_unverified", False),
        )
        items.append(item)
        grouped[r["store"]].append(item)
        total += item.effective_price

    return ShoppingRecommendationResponseDTO(
        items=items,
        not_found=not_found,
        grouped_by_store=dict(grouped),
        total_estimated=total,
    )
