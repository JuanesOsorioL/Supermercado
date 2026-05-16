from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict


class MessageDTO(BaseModel):
    message: str
    source: str
    destination: str


class ChatRequestDTO(BaseModel):
    message: str
    user_id: str
    mime_type: Optional[str] = None
    file_base64: Optional[str] = None


class ChatResponseDTO(BaseModel):
    answer: str


# ═══════════════════════════════════════════════════════════════
# PRICE EXTRACTION
# ═══════════════════════════════════════════════════════════════

class PriceExtractRequestDTO(BaseModel):
    user_id: str
    store: Optional[str] = None
    message: Optional[str] = None
    mime_type: str
    file_base64: str


class PriceExtractResponseDTO(BaseModel):
    store: str
    product_raw: Optional[str] = None
    price: Optional[int] = None
    unit_price: Optional[float] = None
    unit_type: Optional[str] = None
    quantity: Optional[int] = None
    volume_ml: Optional[int] = None
    barcode: Optional[str] = None
    plu: Optional[str] = None
    is_promo: bool = False
    # Discount fields
    original_price: Optional[int] = None
    has_discount: bool = False
    discounted_price: Optional[int] = None
    # Meta
    ocr_confidence: float = 0.0
    needs_confirmation: bool = False
    confirmation_fields: List[str] = []
    saved: bool = False
    observation_id: Optional[int] = None
    extraction_method: str = "ocr"  # "llm_vision" | "ocr"
    product_source: str = "ocr"    # "catalog" | "llm_vision" | "ocr"
    promo_start_date: Optional[str] = None
    promo_end_date: Optional[str] = None
    # Barcode conflict: both sources found different values → frontend must ask user
    barcode_llm: Optional[str] = None
    barcode_ocr: Optional[str] = None
    barcode_conflict: bool = False


class ConfirmPriceRequestDTO(BaseModel):
    observation_id: int
    user_id: str
    confirmed_price: Optional[int] = None
    confirmed_product: Optional[str] = None
    confirmed_store: Optional[str] = None
    confirmed_barcode: Optional[str] = None
    confirmed_has_discount: Optional[bool] = None
    confirmed_discounted_price: Optional[int] = None
    confirmed_volume_ml: Optional[int] = None
    confirmed_promo_end_date: Optional[str] = None


class ConfirmPriceResponseDTO(BaseModel):
    success: bool
    message: str


class SavePriceRequestDTO(BaseModel):
    """Payload para guardar una observación confirmada por el usuario."""
    user_id: str
    store: str
    product_raw: Optional[str] = None
    price: Optional[int] = None
    has_discount: bool = False
    original_price: Optional[int] = None
    discounted_price: Optional[int] = None
    barcode: Optional[str] = None
    volume_ml: Optional[int] = None
    promo_end_date: Optional[str] = None
    extraction_method: Optional[str] = "confirmed"
    ocr_confidence: Optional[float] = 1.0


# ═══════════════════════════════════════════════════════════════
# SEARCH / QUERY
# ═══════════════════════════════════════════════════════════════

class ProductQueryRequestDTO(BaseModel):
    query: str
    user_id: Optional[str] = None
    store: Optional[str] = None
    limit: int = 10


class ProductPriceInfo(BaseModel):
    product_name: str
    store: str
    price: int
    unit_price: Optional[float] = None
    unit_type: Optional[str] = None
    is_promo: bool = False
    has_discount: bool = False
    original_price: Optional[int] = None
    discounted_price: Optional[int] = None
    last_seen: datetime
    barcode: Optional[str] = None


class ProductQueryResponseDTO(BaseModel):
    query: str
    results: List[ProductPriceInfo] = []
    cheapest_store: Optional[str] = None
    cheapest_price: Optional[int] = None
    message: str = ""


# ═══════════════════════════════════════════════════════════════
# COMPARISON
# ═══════════════════════════════════════════════════════════════

class PriceComparisonRequestDTO(BaseModel):
    product_name: Optional[str] = None
    barcode: Optional[str] = None
    user_id: Optional[str] = None


class StorePrice(BaseModel):
    store: str
    price: int
    unit_price: Optional[float] = None
    is_promo: bool = False
    has_discount: bool = False
    original_price: Optional[int] = None
    discounted_price: Optional[int] = None
    last_updated: datetime


class PriceComparisonResponseDTO(BaseModel):
    product_name: str
    barcode: Optional[str] = None
    stores: List[StorePrice] = []
    cheapest: Optional[StorePrice] = None
    most_expensive: Optional[StorePrice] = None
    savings: Optional[int] = None


# ═══════════════════════════════════════════════════════════════
# SESSIONS
# ═══════════════════════════════════════════════════════════════

class SessionStartRequestDTO(BaseModel):
    user_id: str
    store: str


class SessionProductDTO(BaseModel):
    observation_id: int
    position: int
    product_normalized: str
    product_raw: Optional[str] = None
    price: Optional[int] = None
    has_discount: bool = False
    original_price: Optional[int] = None
    discounted_price: Optional[int] = None
    days_ago: int = 0
    promo_end_date: Optional[str] = None
    barcode: Optional[str] = None
    volume_ml: Optional[int] = None


class SessionStartResponseDTO(BaseModel):
    session_id: int
    store: str
    products: List[SessionProductDTO] = []
    message: str = ""


class SessionUpdateProductRequestDTO(BaseModel):
    session_id: int
    observation_id: int
    user_id: str
    price: Optional[int] = None
    has_discount: Optional[bool] = None
    discounted_price: Optional[int] = None


class SessionEndRequestDTO(BaseModel):
    session_id: int
    user_id: str


class SessionEndResponseDTO(BaseModel):
    success: bool
    updated_count: int
    total_count: int
    unverified: List[str] = []
    expired_products: List["SessionProductDTO"] = []


# ═══════════════════════════════════════════════════════════════
# SHOPPING LIST
# ═══════════════════════════════════════════════════════════════

class ShoppingListAddRequestDTO(BaseModel):
    user_id: str
    products: List[str]


class ShoppingListRemoveRequestDTO(BaseModel):
    user_id: str
    product: str


class ShoppingRecommendationItemDTO(BaseModel):
    product: str
    store: str
    price: int
    has_discount: bool = False
    discounted_price: Optional[int] = None
    effective_price: int
    discount_unverified: bool = False


class ShoppingRecommendationResponseDTO(BaseModel):
    items: List[ShoppingRecommendationItemDTO] = []
    not_found: List[str] = []
    grouped_by_store: Dict[str, List[ShoppingRecommendationItemDTO]] = {}
    total_estimated: int = 0
