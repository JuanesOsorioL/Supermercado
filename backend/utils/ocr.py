"""
OCR utilities for extracting price information from supermarket labels.
Uses multiple strategies and regions of interest for better accuracy.
"""

import re
import cv2
import numpy as np
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

try:
    import pytesseract
    # Ajustar path de Tesseract según OS
    import platform
    if platform.system() == "Windows":
        pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
except ImportError:
    pytesseract = None

try:
    from pyzbar.pyzbar import decode as zbar_decode
except ImportError:
    zbar_decode = None


# ═══════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ═══════════════════════════════════════════════════════════════

# Ruido que nunca es precio
PUNTOS_RE    = re.compile(r"\d[\d.,]+\s*puntos", re.IGNORECASE)       # "6,158 puntos"
BARCODE_RE   = re.compile(r"\b\d{12,13}\b")                            # EAN-12/13
PLU_LINE_RE  = re.compile(r"PLU\s*:?\s*\d{4,8}", re.IGNORECASE)       # PLU: 3380813
CODE_LINE_RE = re.compile(r"\b\d{3}\s+\d{6}\s+[A-Z]\s+\w+\b")        # "083 220226 A C04N01P01"
# Número de volumen/peso — "7800 ml", "7800 mi", "7800 m|", "1.8 lt", "250g" — NUNCA es precio
# Incluye variaciones de OCR: "mi", "m|", "m!" por "ml"
VOLUME_NOISE_RE = re.compile(
    r"\d[\d.,]*\s*(?:ml|mi\b|m[|!l]\b|cc|lt|litros?|lts|kg|gr?|mg|oz|und|unid)",
    re.IGNORECASE
)

# Precio unitario "ML A $104.46", "GRAMO a $9.80", "MILILITRO a $5.53" — NUNCA es el precio de venta
UNIT_PRICE_NOISE_RE = re.compile(
    r"(?:MILILITROS?|ML|GRAMOS?|G|GR?|KG|LT|LITROS?|CC|UNID?)\s*[A@]\s*\$?\s*[\d.,]+",
    re.IGNORECASE
)

# Descuento en porcentaje: "25% dcto", "15% dscto", "30% OFF"
DISCOUNT_PERCENT_PATTERN = re.compile(
    r"(\d{1,2})\s*%\s*(?:dcto|dscto|descuento|off)",
    re.IGNORECASE
)

# Precio principal: prioridad a "Llévalo por $X" (OR tipo A), luego formatos estándar
PRICE_PATTERNS = [
    re.compile(r"[Ll]l[eé]valo\s+por\s+\$\s*(\d[\d.,]+)", re.IGNORECASE),  # OR tipo A
    re.compile(r"\$\s*(\d{1,3}(?:[.,]\d{3})+)", re.IGNORECASE),              # $35.350
    re.compile(r"\$\s*(\d{4,6})", re.IGNORECASE),                             # $35350
    re.compile(r"(\d{1,3}(?:[.,]\d{3})+)\s*(?:pesos|cop)?", re.IGNORECASE),  # 35.350
]


def clean_for_price(text: str) -> str:
    """Strip noise (points, barcodes, PLU, volumes, unit prices) so they don't get parsed as prices."""
    text = PUNTOS_RE.sub("", text)
    text = BARCODE_RE.sub("", text)
    text = PLU_LINE_RE.sub("", text)
    text = CODE_LINE_RE.sub("", text)
    text = UNIT_PRICE_NOISE_RE.sub("", text)  # "ML A $104.46" → remove BEFORE volume strip
    text = VOLUME_NOISE_RE.sub("", text)       # "7800 ml" → "" (evita leer volumen como precio)
    return text

# Precio unitario: ML A $5.36 o $5.36/ML
UNIT_PRICE_PATTERNS = [
    re.compile(r"(?:ML|G|KG|LT|UNID?)\s*[A@]\s*\$?\s*(\d+[.,]?\d*)", re.IGNORECASE),
    re.compile(r"\$?\s*(\d+[.,]?\d*)\s*/\s*(?:ML|G|KG|LT|UNID?)", re.IGNORECASE),
    re.compile(r"(?:PRECIO|P)\s*(?:POR|X)\s*(?:ML|G|KG)\s*[:=]?\s*\$?\s*(\d+[.,]?\d*)", re.IGNORECASE),
]

# Código de barras EAN-13
EAN13_RE = re.compile(r"\b[0-9]{13}\b")
EAN12_RE = re.compile(r"\b[0-9]{12}\b")

# PLU
PLU_PATTERNS = [
    re.compile(r"PLU\s*[:=]?\s*(\d{4,6})", re.IGNORECASE),
    re.compile(r"COD(?:IGO)?\s*[:=]?\s*(\d{4,6})", re.IGNORECASE),
]

# Cantidad/Volumen
QUANTITY_PATTERNS = [
    re.compile(r"[x×*]\s*(\d+)\s*(?:UND|UNID|UNIDADES)", re.IGNORECASE),
    re.compile(r"(\d+)\s*(?:UND|UNID|UNIDADES)", re.IGNORECASE),
]

VOLUME_PATTERNS = [
    re.compile(r"[x×*]\s*(\d+)\s*(?:ML|CC)", re.IGNORECASE),
    re.compile(r"(\d+)\s*(?:ML|CC)", re.IGNORECASE),
    re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:LT|LITROS?)", re.IGNORECASE),
]


# Validez de promoción: "Válido del 30/04/2026 al 10/05/2026"
PROMO_DATE_PATTERN = re.compile(
    r"v[aá]li[do]{2}\s+del?\s+(\d{1,2}/\d{1,2}/\d{4})\s+al?\s+(\d{1,2}/\d{1,2}/\d{4})",
    re.IGNORECASE
)

# Precios de descuento: "Antes: $12.900 Ahora: $9.900", "precio regular $X precio socio $Y"
DISCOUNT_PATTERNS = [
    re.compile(r"antes[:\s]+\$?\s*(\d[\d.,]+).*?(?:ahora|precio)[:\s]+\$?\s*(\d[\d.,]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"precio\s+(?:regular|normal)[:\s]+\$?\s*(\d[\d.,]+).*?precio\s+(?:socio|con\s+tarjeta|descuento)[:\s]+\$?\s*(\d[\d.,]+)", re.IGNORECASE | re.DOTALL),
    re.compile(r"(\d[\d.,]{3,})\s+(?:tachado|antes)\s+(\d[\d.,]{3,})", re.IGNORECASE),
    re.compile(r"\$\s*(\d[\d.,]+)\s+\$\s*(\d[\d.,]+)", re.IGNORECASE),  # Dos precios seguidos
]


def parse_discount_prices(text: str):
    """
    Extract original price and discounted price from text.
    Returns (original_price, discounted_price) or (None, None).
    """
    if not text:
        return None, None

    for pattern in DISCOUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            p1_str = re.sub(r"[^\d]", "", match.group(1))
            p2_str = re.sub(r"[^\d]", "", match.group(2))
            if p1_str and p2_str:
                p1, p2 = int(p1_str), int(p2_str)
                if 500 <= p1 <= 999999 and 500 <= p2 <= 999999 and p1 != p2:
                    # Original is the higher price, discounted the lower
                    return (max(p1, p2), min(p1, p2))

    return None, None


@dataclass
class OCRResult:
    """Result from OCR extraction."""
    product_raw: Optional[str] = None
    product_normalized: Optional[str] = None
    price: Optional[int] = None
    unit_price: Optional[float] = None
    unit_type: Optional[str] = None
    quantity: Optional[int] = None
    volume_ml: Optional[int] = None
    barcode: Optional[str] = None
    plu: Optional[str] = None
    is_promo: bool = False
    has_discount: bool = False
    original_price: Optional[int] = None
    discounted_price: Optional[int] = None
    promo_start_date: Optional[str] = None
    promo_end_date: Optional[str] = None
    confidence: float = 0.0
    needs_confirmation: bool = False
    confirmation_fields: List[str] = None
    raw_text: str = ""

    def __post_init__(self):
        if self.confirmation_fields is None:
            self.confirmation_fields = []


# ═══════════════════════════════════════════════════════════════
# IMAGE PREPROCESSING
# ═══════════════════════════════════════════════════════════════

def preprocess_image(img: np.ndarray, mode: str = "standard") -> np.ndarray:
    """
    Preprocess image for OCR with different modes.
    
    Modes:
    - standard: General preprocessing
    - price: Optimized for large price numbers
    - text: Optimized for product text
    - barcode: Optimized for barcode reading
    """
    if img is None:
        return None
    
    # Convert to grayscale if needed
    if len(img.shape) == 3:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img.copy()
    
    if mode == "standard":
        # Resize for better OCR
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        # Denoise
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        # Adaptive threshold
        processed = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 11, 2
        )
    
    elif mode == "price":
        # Scale up more for large numbers
        gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        # Stronger contrast
        gray = cv2.convertScaleAbs(gray, alpha=1.5, beta=0)
        # Binary threshold (Otsu)
        _, processed = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    elif mode == "text":
        # Standard scale
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        # Mild denoise
        gray = cv2.bilateralFilter(gray, 9, 75, 75)
        # Adaptive threshold
        processed = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 15, 4
        )
    
    elif mode == "barcode":
        # Keep original size, just threshold
        _, processed = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    
    else:
        processed = gray
    
    return processed


def crop_roi(img: np.ndarray, region: str) -> np.ndarray:
    """
    Crop a region of interest from the image.
    
    Regions (based on typical supermarket labels):
    - header: Top area with product name
    - price: Right side with main price
    - price_unit: Left side with unit price
    - plu: Bottom left with PLU code
    - barcode: Bottom with barcode
    """
    h, w = img.shape[:2]
    
    regions = {
        "header": (0, 0, w, int(h * 0.25)),           # Top 25%
        "price": (int(w * 0.50), int(h * 0.30), w, int(h * 0.90)),  # Right middle
        "price_unit": (0, int(h * 0.20), int(w * 0.50), int(h * 0.40)),  # Left upper-middle
        "plu": (0, int(h * 0.55), int(w * 0.45), h),  # Bottom left
        "barcode": (0, int(h * 0.45), int(w * 0.60), int(h * 0.75)),  # Middle-bottom left
        "full": (0, 0, w, h),  # Full image
    }
    
    if region not in regions:
        return img
    
    x1, y1, x2, y2 = regions[region]
    return img[y1:y2, x1:x2]


# ═══════════════════════════════════════════════════════════════
# OCR FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def run_ocr(img: np.ndarray, config: str = "") -> Tuple[str, float]:
    """Run Tesseract OCR on an image."""
    if pytesseract is None:
        return "", 0.0
    
    try:
        # Default config
        if not config:
            config = "--oem 3 --psm 6"
        
        text = pytesseract.image_to_string(img, lang="spa", config=config)
        
        # Calculate simple confidence based on alphanumeric chars
        clean = "".join(c for c in text if c.isalnum() or c in "$.:,")
        confidence = min(1.0, len(clean) / 100.0) if clean else 0.0
        
        return text.strip(), confidence
    except Exception as e:
        print(f"OCR error: {e}")
        return "", 0.0


def run_ocr_digits(img: np.ndarray) -> str:
    """Run OCR optimized for digits only — tries PSM 7 and PSM 6 and returns best."""
    if pytesseract is None:
        return ""

    whitelist = "-c tessedit_char_whitelist=0123456789.$,."
    best = ""
    for psm in (7, 6, 11):
        try:
            text = pytesseract.image_to_string(img, config=f"--oem 3 --psm {psm} {whitelist}").strip()
            if len(text) > len(best):
                best = text
        except Exception:
            continue
    return best


def read_barcode(img: np.ndarray) -> Optional[str]:
    """Read barcode from image using pyzbar."""
    if zbar_decode is None:
        return None
    
    try:
        # Try original image
        codes = zbar_decode(img)
        for code in codes:
            data = code.data.decode("utf-8", errors="ignore").strip()
            if data.isdigit() and len(data) in (12, 13, 14):
                return data
        
        # Try grayscale
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            codes = zbar_decode(gray)
            for code in codes:
                data = code.data.decode("utf-8", errors="ignore").strip()
                if data.isdigit() and len(data) in (12, 13, 14):
                    return data
        
        return None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════
# PARSING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def parse_price(text: str) -> Optional[int]:
    """
    Extract the main price from label text.
    Strategy: collect ALL plausible COP prices and return the LARGEST
    (the main shelf price is always the biggest number on the label).
    """
    if not text:
        return None

    # Priority: "Llévalo por $X" (OR tipo A) — explicit main price, take it directly
    llevalo = PRICE_PATTERNS[0].search(text)
    if llevalo:
        digits = re.sub(r"[^\d]", "", llevalo.group(1))
        if digits and 1000 <= int(digits) <= 1999999:
            return int(digits)

    candidates: list = []

    # Dollar-prefixed numbers: $43.100 / $43,100 / $35350
    for m in re.finditer(r"\$\s*(\d{1,3}(?:[.,]\d{3})+|\d{4,6})", text):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            val = int(digits)
            if 1000 <= val <= 1999999:
                candidates.append(val)

    # Formatted thousands even without $: 43.100 / 43,100
    # Use (?<!\d) / (?!\d) instead of \b — \b fails when number is adjacent to letters
    # (e.g. OCR produces "43,100MILILITRO" with no space)
    for m in re.finditer(r"(?<!\d)(\d{1,3}[.,]\d{3})(?!\d)", text):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if digits:
            val = int(digits)
            if 1000 <= val <= 1999999:
                candidates.append(val)

    # Plain 4-6 digit numbers as last resort
    for m in re.finditer(r"(?<!\d)(\d{4,6})(?!\d)", text):
        val = int(m.group(1))
        if 1000 <= val <= 199999:
            candidates.append(val)

    return max(candidates) if candidates else None


def parse_unit_price(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract unit price and unit type from text."""
    if not text:
        return None, None
    
    for pattern in UNIT_PRICE_PATTERNS:
        match = pattern.search(text)
        if match:
            price_str = match.group(1).replace(",", ".")
            try:
                price = float(price_str)
                # Determine unit type
                upper_text = text.upper()
                if "ML" in upper_text or "CC" in upper_text:
                    return price, "ML"
                elif "KG" in upper_text:
                    return price, "KG"
                elif "G" in upper_text:
                    return price, "G"
                elif "LT" in upper_text or "LITRO" in upper_text:
                    return price, "LT"
                else:
                    return price, "UNIDAD"
            except ValueError:
                continue
    
    return None, None


def parse_plu(text: str) -> Optional[str]:
    """Extract PLU code from text."""
    if not text:
        return None
    
    for pattern in PLU_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    
    return None


def parse_barcode_from_text(text: str) -> Optional[str]:
    """Extract barcode (EAN-13) from OCR text."""
    if not text:
        return None

    match = EAN13_RE.search(text)
    if match:
        return match.group(0)

    match = EAN12_RE.search(text)
    if match:
        return match.group(0)

    return None


def extract_barcode_from_bottom(img: np.ndarray) -> Optional[str]:
    """
    Extract barcode number printed below the bars.
    Crops bottom 30% of image and runs digit-only OCR — much more reliable
    than searching the full garbled OCR text.
    """
    if img is None:
        return None
    h, w = img.shape[:2]
    strip = img[int(h * 0.65):, :]  # bottom 35%

    # Try digit OCR first (fast)
    processed = preprocess_image(strip, "price")
    text = run_ocr_digits(processed)
    barcode = parse_barcode_from_text(text)
    if barcode:
        return barcode

    # Try text OCR (slower but better at reading printed numbers)
    text, _ = run_ocr(preprocess_image(strip, "text"))
    return parse_barcode_from_text(text)


def parse_quantity(text: str) -> Optional[int]:
    """Extract quantity (number of units) from text."""
    if not text:
        return None
    
    for pattern in QUANTITY_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    
    return None


def parse_volume(text: str) -> Optional[int]:
    """Extract volume in ML from text."""
    if not text:
        return None
    
    for pattern in VOLUME_PATTERNS:
        match = pattern.search(text)
        if match:
            value_str = match.group(1).replace(",", ".")
            value = float(value_str)
            # Convert liters to ML
            if "LT" in text.upper() or "LITRO" in text.upper():
                if value < 10:  # Probably liters
                    value *= 1000
            return int(value)
    
    return None


def extract_product_name(text: str) -> Optional[str]:
    """Extract product name from OCR text."""
    if not text:
        return None
    
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    
    if not lines:
        return None
    
    # Usually the first or longest line is the product name
    # Filter out lines that look like prices or codes
    name_candidates = []
    for line in lines[:3]:  # Check first 3 lines
        # Skip if mostly digits
        alpha_ratio = sum(c.isalpha() for c in line) / max(len(line), 1)
        if alpha_ratio < 0.3:
            continue
        # Skip if looks like price
        if "$" in line or re.match(r"^\d{4,}$", line.replace(".", "").replace(",", "")):
            continue
        name_candidates.append(line)
    
    if name_candidates:
        # Return longest candidate
        return max(name_candidates, key=len)[:120]
    
    # Fallback: first line
    return lines[0][:120] if lines else None


def detect_promo(img: np.ndarray, threshold: float = 0.03) -> bool:
    """Detect if image has promotional (red) markers."""
    if img is None or len(img.shape) != 3:
        return False
    
    try:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        
        # Red color ranges in HSV
        lower1 = np.array([0, 120, 70])
        upper1 = np.array([10, 255, 255])
        lower2 = np.array([170, 120, 70])
        upper2 = np.array([180, 255, 255])
        
        mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
        red_pixels = int(np.count_nonzero(mask))
        total = img.shape[0] * img.shape[1]
        
        return (red_pixels / max(1, total)) >= threshold
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════
# MAIN EXTRACTION FUNCTION
# ═══════════════════════════════════════════════════════════════

def extract_price_info(img: np.ndarray) -> OCRResult:
    """
    Extract all price information from a supermarket label image.
    Uses multiple strategies and regions for best accuracy.
    """
    result = OCRResult()
    
    if img is None:
        result.needs_confirmation = True
        result.confirmation_fields = ["price", "product"]
        return result
    
    # ═══════════════════════════════════════════════════════════════
    # 1. BARCODE (most reliable identifier)
    # ═══════════════════════════════════════════════════════════════
    result.barcode = read_barcode(img)

    if not result.barcode:
        barcode_roi = crop_roi(img, "barcode")
        result.barcode = read_barcode(barcode_roi)

    # Dedicated bottom-strip digit OCR — reads the number printed below bars
    if not result.barcode:
        result.barcode = extract_barcode_from_bottom(img)
    
    # ═══════════════════════════════════════════════════════════════
    # 2. FULL IMAGE OCR (for product name and general text)
    # ═══════════════════════════════════════════════════════════════
    processed_full = preprocess_image(img, "text")
    full_text, full_conf = run_ocr(processed_full)
    result.raw_text = full_text
    result.confidence = full_conf
    
    # Extract product name from full text
    result.product_raw = extract_product_name(full_text)
    
    # Try to get barcode from text if not found visually
    if not result.barcode:
        result.barcode = parse_barcode_from_text(full_text)
    
    # ═══════════════════════════════════════════════════════════════
    # 3. PRICE REGION OCR
    # ═══════════════════════════════════════════════════════════════
    price_roi = crop_roi(img, "price")
    processed_price = preprocess_image(price_roi, "price")
    price_text = run_ocr_digits(processed_price)
    
    # Parse main price — always try both sources and take max
    clean_full = clean_for_price(full_text)
    price_from_roi  = parse_price(price_text) if price_text else None
    price_from_full = parse_price(clean_full)

    if price_from_roi and price_from_full:
        result.price = max(price_from_roi, price_from_full)
    else:
        result.price = price_from_roi or price_from_full
    
    # ═══════════════════════════════════════════════════════════════
    # 4. UNIT PRICE REGION
    # ═══════════════════════════════════════════════════════════════
    unit_roi = crop_roi(img, "price_unit")
    processed_unit = preprocess_image(unit_roi, "text")
    unit_text, _ = run_ocr(processed_unit)
    
    result.unit_price, result.unit_type = parse_unit_price(unit_text)
    
    # Fallback: try full text
    if not result.unit_price:
        result.unit_price, result.unit_type = parse_unit_price(full_text)
    
    # ═══════════════════════════════════════════════════════════════
    # 5. PLU REGION
    # ═══════════════════════════════════════════════════════════════
    plu_roi = crop_roi(img, "plu")
    processed_plu = preprocess_image(plu_roi, "standard")
    plu_text, _ = run_ocr(processed_plu)
    
    result.plu = parse_plu(plu_text)
    
    # Fallback: try full text
    if not result.plu:
        result.plu = parse_plu(full_text)
    
    # ═══════════════════════════════════════════════════════════════
    # 6. QUANTITY AND VOLUME
    # ═══════════════════════════════════════════════════════════════
    result.quantity = parse_quantity(full_text)
    result.volume_ml = parse_volume(full_text)
    
    # ═══════════════════════════════════════════════════════════════
    # 7. DISCOUNT DETECTION (two prices on same label)
    # Use clean_full so unit prices ("ML A $104.46") are not confused with retail prices
    # ═══════════════════════════════════════════════════════════════
    original_price, discounted_price = parse_discount_prices(clean_full)
    if original_price and discounted_price:
        result.has_discount = True
        result.original_price = original_price
        result.discounted_price = discounted_price
        if not result.price:
            result.price = discounted_price  # effective price = discounted

    # ═══════════════════════════════════════════════════════════════
    # 7b. PERCENTAGE DISCOUNT ("25% dcto" → calculate discounted price)
    # ═══════════════════════════════════════════════════════════════
    if not result.has_discount and result.price:
        pct_match = DISCOUNT_PERCENT_PATTERN.search(full_text)
        if pct_match:
            pct = int(pct_match.group(1))
            if 5 <= pct <= 70:  # sanity: realistic discount range
                discounted = round(result.price * (1 - pct / 100))
                if discounted >= 500:
                    result.has_discount = True
                    result.original_price = result.price
                    result.discounted_price = discounted
                    # price stays as original (user sees original → discounted)

    # ═══════════════════════════════════════════════════════════════
    # 7d. PROMO DATE DETECTION
    # ═══════════════════════════════════════════════════════════════
    date_match = PROMO_DATE_PATTERN.search(full_text)
    if date_match:
        result.promo_start_date = date_match.group(1)
        result.promo_end_date   = date_match.group(2)

    # ═══════════════════════════════════════════════════════════════
    # 8. PROMOTION DETECTION (red label color)
    # ═══════════════════════════════════════════════════════════════
    result.is_promo = result.has_discount or detect_promo(img)
    
    # ═══════════════════════════════════════════════════════════════
    # 8. CONFIDENCE CHECK - Determine if confirmation needed
    # ═══════════════════════════════════════════════════════════════
    confirmation_fields = []
    
    if not result.price:
        confirmation_fields.append("price")
    elif result.price < 500 or result.price > 500000:
        confirmation_fields.append("price")  # Suspicious price
    
    if not result.product_raw or len(result.product_raw) < 5:
        confirmation_fields.append("product")
    
    if result.confidence < 0.3:
        if "price" not in confirmation_fields:
            confirmation_fields.append("price")
        if "product" not in confirmation_fields:
            confirmation_fields.append("product")
    
    result.confirmation_fields = confirmation_fields
    result.needs_confirmation = len(confirmation_fields) > 0
    
    # Normalize product name
    if result.product_raw:
        from .database import normalize_product_name
        result.product_normalized = normalize_product_name(result.product_raw)
    
    return result
