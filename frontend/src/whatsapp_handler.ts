import P from "pino";
import * as QRCode from "qrcode";
import {
  makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  WASocket,
  AuthenticationState,
  downloadMediaMessage,
  getContentType,
  fetchLatestBaileysVersion,
} from "@whiskeysockets/baileys";

import { createWriteStream, readFileSync, existsSync, rmSync, unlinkSync } from "fs";
import { join } from "path";
import { tmpdir } from "os";

// ═══════════════════════════════════════════════════════════════
// TYPES
// ═══════════════════════════════════════════════════════════════

interface PriceExtractResponse {
  store: string;
  product_raw?: string;
  price?: number;
  unit_price?: number;
  unit_type?: string;
  quantity?: number;
  volume_ml?: number;
  barcode?: string;
  plu?: string;
  is_promo: boolean;
  has_discount: boolean;
  original_price?: number;
  discounted_price?: number;
  ocr_confidence: number;
  needs_confirmation: boolean;
  confirmation_fields: string[];
  saved: boolean;
  observation_id?: number;
  extraction_method: string;
  product_source: string; // "catalog" | "llm_vision" | "ocr"
  promo_start_date?: string;
  promo_end_date?: string;
  barcode_llm?: string;
  barcode_ocr?: string;
  barcode_conflict?: boolean;
}

interface SessionProduct {
  observation_id: number;
  position: number;
  product_normalized: string;
  product_raw?: string;
  price?: number;
  has_discount: boolean;
  original_price?: number;
  discounted_price?: number;
  days_ago: number;
  promo_end_date?: string;
  barcode?: string;
  volume_ml?: number;
}

type SessionEditField = "nombre" | "precio" | "descuento" | null;

interface ActiveSession {
  session_id: number;
  store: string;
  products: SessionProduct[];
  viewingProduct?: number;
  editingSessionField?: SessionEditField;
  reviewingExpired?: boolean;
}

interface SessionEndResponse {
  success: boolean;
  updated_count: number;
  total_count: number;
  unverified: string[];
  expired_products: SessionProduct[];
}

// Subset of PriceExtractResponse that the user can correct in the interactive menu
interface EditableData {
  store: string;
  product_raw: string;
  price?: number;
  has_discount: boolean;
  original_price?: number;
  discounted_price?: number;
  barcode?: string;
  volume_ml?: number;
  promo_end_date?: string;
}

type EditingField = "price" | "discount" | "product" | "barcode" | "store" | "promo_date" | null;

interface PendingConfirmation {
  observation_id?: number;   // undefined until user confirms and calls /api/price/save
  fields: string[];
  extracted: PriceExtractResponse;
  timestamp: number;
  currentData: EditableData;       // live copy tracking in-progress edits
  editingField: EditingField;      // which field is awaiting a typed value (null = showing menu)
  lastPromptMsgKey?: { remoteJid: string; fromMe: boolean; id: string };
  awaitingBarcodeSelection?: boolean;
  barcodeOptions?: [string | null, string | null];  // [llm, ocr]
}

// ── Helper: initialize EditableData from a backend response ───────────────────
function extractableToEditable(r: PriceExtractResponse): EditableData {
  return {
    store: r.store,
    product_raw: r.product_raw ?? "No detectado",
    price: r.price,
    has_discount: r.has_discount,
    original_price: r.original_price,
    discounted_price: r.discounted_price,
    barcode: r.barcode,
    volume_ml: r.volume_ml,
    promo_end_date: r.promo_end_date,
  };
}

function numEmoji(n: number): string {
  const e = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"];
  return n >= 1 && n <= 10 ? e[n - 1] : `${n}.`;
}

// ═══════════════════════════════════════════════════════════════
// KNOWN STORES (detection map)
// ═══════════════════════════════════════════════════════════════

const STORE_ALIASES: Record<string, string> = {
  "la2000": "la2000", "2000": "la2000",
  "exito": "exito", "éxito": "exito", "exito express": "exito",
  "d1": "d1",
  "ara": "ara",
  "jumbo": "jumbo",
  "olimpica": "olimpica", "olímpica": "olimpica",
  "makro": "makro",
  "carulla": "carulla",
  "metro": "metro",
  "surtimax": "surtimax",
  "alkosto": "alkosto",
  "ktronix": "ktronix",
  "pricesmart": "pricesmart",
  // Nuevos supermercados
  "or": "or", "mercados or": "or", "or supermercados": "or",
  "euro": "euro", "supermercados euro": "euro", "euro supermercados": "euro",
};

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════

function fileToBase64(path: string): string {
  return readFileSync(path).toString("base64");
}

function detectStore(text: string): string | null {
  const t = text.toLowerCase().trim();
  for (const [alias, canonical] of Object.entries(STORE_ALIASES)) {
    if (t.includes(alias)) return canonical;
  }
  return null;
}

/** Detect "estoy en X", "llegué al X", "vine al X", "fui a X", "estoy en X supermercado" */
function detectStoreArrival(text: string): string | null {
  const t = text.toLowerCase().trim();
  const arrivalPhrases = [
    /(?:estoy en|llegué al?|llegue al?|vine al?|fui al?|estoy yendo al?)\s+(?:el\s+|la\s+)?([a-z0-9\s]+)/i,
    /(?:en el|en la)\s+([a-z0-9\s]+)\s+(?:ahora|ya)/i,
  ];

  for (const pattern of arrivalPhrases) {
    const match = t.match(pattern);
    if (match) {
      const candidate = match[1].trim().split(/\s+/).slice(0, 2).join(" ");
      const store = detectStore(candidate) || detectStore(match[1].trim());
      if (store) return store;
    }
  }

  // Also detect simple "tienda:" format
  const m = t.match(/tienda\s*:\s*([a-z0-9_-]+)/i);
  if (m?.[1]) return STORE_ALIASES[m[1].toLowerCase()] || m[1].toLowerCase();

  return null;
}


function extractStore(text: string): string {
  const t = (text || "").trim().toLowerCase();
  const m = t.match(/store\s*:\s*([a-z0-9_-]+)/i);
  if (m?.[1]) return STORE_ALIASES[m[1].toLowerCase()] || m[1].toLowerCase();

  const first = t.split(/\s+/)[0];
  if (first && STORE_ALIASES[first]) return STORE_ALIASES[first];

  return "desconocido";
}

function parseIntent(text: string): { intent: string; query: string; store?: string } {
  const t = (text || "").trim().toLowerCase();

  // Session end
  if (/^(listo|fin|terminé|termine|sali|salí|cerrar)$/.test(t)) {
    return { intent: "session_end", query: "" };
  }

  // Products list / catalog
  if (/^ver productos?$/i.test(t) || /^(ver\s+)?cat[aá]logo$/i.test(t)) return { intent: "ver_productos", query: "" };

  // Shopping list commands
  if (/^(quiero mercar|hacer mercado|mercado|mi lista de compras|recomendar)/.test(t)) {
    return { intent: "shopping_recommend", query: "" };
  }
  if (/^agregar?\s+(.+)/.test(t)) {
    const match = t.match(/^agregar?\s+(.+)/);
    return { intent: "list_add", query: match?.[1] || "" };
  }
  if (/^quitar\s+(.+)/.test(t)) {
    const match = t.match(/^quitar\s+(.+)/);
    return { intent: "list_remove", query: match?.[1] || "" };
  }
  if (/^(mi lista|ver lista|lista)$/.test(t)) {
    return { intent: "list_get", query: "" };
  }
  if (/^limpiar\s+lista$/i.test(t)) {
    return { intent: "list_clear", query: "" };
  }

  // Comparison queries
  if (t.includes("comparar") || t.includes("compare") || t.includes(" vs ")) {
    const query = t.replace(/comparar|compare|\bvs\b|precio|precios|de/gi, "").trim();
    return { intent: "compare", query };
  }

  // Cheapest
  if (t.includes("más barato") || t.includes("mas barato") || t.includes("barato") || t.includes("mejor precio")) {
    const query = t.replace(/más barato|mas barato|barato|mejor precio|dónde|donde|está|esta/gi, "").trim();
    return { intent: "cheapest", query };
  }

  // Search
  if (t.includes("buscar") || t.includes("busca") || t.includes("precio de") || t.includes("cuánto cuesta") || t.includes("cuanto cuesta")) {
    const query = t.replace(/buscar|busca|precio de|cuánto cuesta|cuanto cuesta/gi, "").trim();
    return { intent: "search", query };
  }

  // Admin / testing
  if (/^(limpiar todo|borrar bd|borrar todo|reset bd|clear all)$/i.test(t)) return { intent: "admin_clear", query: "" };
  if (/^(ver catalogo|catalogo|catálogo)$/i.test(t)) return { intent: "admin_catalog", query: "" };

  // Help
  if (/^(ayuda|help|\?|comandos)$/.test(t)) return { intent: "help", query: "" };

  // Stats
  if (/^(stats|estadísticas|estadisticas|resumen)$/.test(t)) return { intent: "stats", query: "" };

  // Confirmation
  if (/^(si|sí|yes|ok|confirmar)$/.test(t)) return { intent: "confirm_yes", query: "" };
  if (/^(no|cancelar|cancel)$/.test(t)) return { intent: "confirm_no", query: "" };

  // Edit commands (always available when there's a pending confirmation)
  // "precio 43100 producto Alquería Sixpack" — both at once
  const editBoth = t.match(/^precio\s+(\d{3,7})\s+producto\s+(.+)$/i);
  if (editBoth) return { intent: "edit_both", query: `${editBoth[1]}|${editBoth[2].trim()}` };

  // "precio 43100"
  const editPrice = t.match(/^precio\s+(\d{3,7})$/i);
  if (editPrice) return { intent: "edit_price", query: editPrice[1] };

  // "producto Alquería Sixpack 7800ml"
  const editProduct = t.match(/^producto\s+(.+)$/i);
  if (editProduct) return { intent: "edit_product", query: editProduct[1].trim() };

  // "codigo 7702177022764"
  const editBarcode = t.match(/^c[oó]digo\s+(\d{8,14})$/i);
  if (editBarcode) return { intent: "edit_barcode", query: editBarcode[1] };

  // "tienda exito"
  const editStore = t.match(/^tienda\s+([a-z0-9\s]+)$/i);
  if (editStore) return { intent: "edit_store", query: editStore[1].trim() };

  // "descuento 38000" — mark as having discount at that price
  const editDiscount = t.match(/^descuento\s+(\d{3,7})$/i);
  if (editDiscount) return { intent: "edit_discount", query: editDiscount[1] };

  // "sin descuento" — remove discount flag
  if (/^sin descuento$/i.test(t)) return { intent: "edit_no_discount", query: "" };

  // "volumen 1800" or "ml 1800"
  const editVolume = t.match(/^(?:volumen|ml|litros?)\s+(\d{2,5})$/i);
  if (editVolume) return { intent: "edit_volume", query: editVolume[1] };

  // Price correction (number only)
  const priceMatch = t.match(/^\$?\s*(\d{1,3}(?:[.,]\d{3})*|\d{4,7})$/);
  if (priceMatch) {
    return { intent: "confirm_price", query: priceMatch[1].replace(/[^\d]/g, "") };
  }

  return { intent: "unknown", query: t };
}

function formatPrice(price: number | undefined): string {
  if (!price) return "No detectado";
  return `$${price.toLocaleString("es-CO")}`;
}

// ═══════════════════════════════════════════════════════════════
// WHATSAPP HANDLER CLASS
// ═══════════════════════════════════════════════════════════════

class WhatsAppHandler {
  private BACKEND_BASE_URL: string = process.env.BACKEND_BASE_URL || "http://127.0.0.1:8000";

  private sock!: WASocket;
  private qrAttempts = 0;
  private readonly maxQrAttempts = 3;
  private saveCreds: () => Promise<void> | null;
  private authState: AuthenticationState | undefined;

  // In-memory state (survives within process lifetime)
  private pendingConfirmations: Map<string, PendingConfirmation> = new Map();
  private activeSessions: Map<string, ActiveSession> = new Map();
  private shoppingListMode: Map<string, string[]> = new Map(); // jid → items en modo edición de lista
  private promoCheckStarted = false;

  constructor() {
    this.saveCreds = async () => {};
    this.onCredsUpdate = this.onCredsUpdate.bind(this);
    this.onMessagesUpsert = this.onMessagesUpsert.bind(this);
    this.onConnectionUpdate = this.onConnectionUpdate.bind(this);
  }

  // ═══════════════════════════════════════════════════════════════
  // SOCKET INITIALIZATION
  // ═══════════════════════════════════════════════════════════════

  async initSocket() {
    console.log("Inicializando socket de WhatsApp...");
    const { state: newState, saveCreds: newSaveCreds } =
      await useMultiFileAuthState("auth_info_baileys");
    this.authState = newState;
    this.saveCreds = newSaveCreds;

    const { version } = await fetchLatestBaileysVersion();

    this.sock = makeWASocket({
      version,
      printQRInTerminal: false,
      auth: this.authState,
      browser: ["Ubuntu", "Chrome", "22.04.4"],
      syncFullHistory: false
    });

    this.sock.ev.on("creds.update", this.onCredsUpdate);
    this.sock.ev.on("messages.upsert", this.onMessagesUpsert);
    this.sock.ev.on("connection.update", this.onConnectionUpdate);
  }

  onCredsUpdate(_: any) {
    this.saveCreds?.();
  }

  // ═══════════════════════════════════════════════════════════════
  // MEDIA HANDLING
  // ═══════════════════════════════════════════════════════════════

  private downloadAndSaveMedia(stream: any, filepath: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const writeStream = createWriteStream(filepath);
      stream.pipe(writeStream);
      writeStream.on("finish", resolve);
      writeStream.on("error", reject);
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // BACKEND API CALLS
  // ═══════════════════════════════════════════════════════════════

  private async safeReply(jid: string, text: string) {
    await this.sock.sendMessage(jid, { text });
  }

  private async callBackend(endpoint: string, payload: any, method: string = "POST"): Promise<any> {
    const url = `${this.BACKEND_BASE_URL}${endpoint}`;
    const options: RequestInit = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (method !== "GET") options.body = JSON.stringify(payload);

    const res = await fetch(url, options);
    const bodyText = await res.text();
    let json: any = null;
    try { json = JSON.parse(bodyText); } catch { json = { raw: bodyText }; }

    if (!res.ok) {
      const detail = json?.detail || json?.raw || bodyText;
      throw new Error(`Backend ${res.status}: ${detail}`);
    }
    return json;
  }

  // ═══════════════════════════════════════════════════════════════
  // SESSION FLOW
  // ═══════════════════════════════════════════════════════════════

  private async handleStoreArrival(jid: string, store: string) {
    try {
      const result = await this.callBackend("/api/session/start", {
        user_id: jid,
        store,
      });

      const session: ActiveSession = {
        session_id: result.session_id,
        store: result.store,
        products: result.products || [],
      };
      this.activeSessions.set(jid, session);
      await this.sendSessionProductList(jid, session);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error iniciando sesión: ${err?.message || "fallo desconocido"}`);
    }
  }

  private async sendSessionProductList(jid: string, session: ActiveSession): Promise<void> {
    if (session.reviewingExpired) {
      let msg = `⚠️ *${session.store.toUpperCase()} — Descuentos a verificar*\n`;
      msg += `_Estos productos tienen descuento vencido o sin fecha registrada:_\n\n`;
      for (const p of session.products) {
        const name = (p.product_normalized || p.product_raw || "?").toUpperCase();
        const priceStr = p.has_discount && p.discounted_price
          ? `${formatPrice(p.discounted_price)} ⚡`
          : formatPrice(p.price);
        const vencio = p.promo_end_date ? ` _(venció ${p.promo_end_date})_` : ` _(sin fecha)_`;
        msg += `${numEmoji(p.position)} ${name} — ${priceStr}${vencio}\n`;
      }
      msg += `\nSelecciona un número para editar.\n*listo* para terminar sin cambios.`;
      await this.safeReply(jid, msg);
      return;
    }

    let msg = `🛒 *${session.store.toUpperCase()}*\n\n`;
    if (!session.products.length) {
      msg += `Sin productos registrados.\nEnvía fotos para agregar.\n\n*listo* para terminar.`;
    } else {
      for (const p of session.products) {
        const priceStr = p.has_discount && p.discounted_price
          ? `${formatPrice(p.discounted_price)} ⚡`
          : formatPrice(p.price);
        msg += `${numEmoji(p.position)} ${p.product_normalized} — ${priceStr}\n`;
      }
      msg += `\nEscribe un número para ver el detalle.\n*listo* para terminar.`;
    }
    await this.safeReply(jid, msg);
  }

  private async sendProductDetailView(jid: string, session: ActiveSession, product: SessionProduct): Promise<void> {
    let msg = `📦 *${product.product_normalized}*\n`;
    if (product.volume_ml) msg += `🥤 ${product.volume_ml} ML\n`;
    if (product.has_discount && product.discounted_price) {
      msg += `💲 Original: ${formatPrice(product.original_price ?? product.price)}\n`;
      msg += `⚡ Descuento: ${formatPrice(product.discounted_price)}\n`;
    } else if (product.price) {
      msg += `💲 Precio: ${formatPrice(product.price)}\n`;
    }
    if (product.promo_end_date) {
      msg += `🗓 Válido hasta: ${product.promo_end_date}\n`;
    }
    if (product.barcode) msg += `🔢 ${product.barcode}\n`;
    msg += `\n1️⃣ nombre\n2️⃣ precio\n3️⃣ descuento\n4️⃣ sin descuento\n5️⃣ volver`;
    await this.safeReply(jid, msg);
  }

  private async handleProductDetailDigit(jid: string, session: ActiveSession, digit: number): Promise<void> {
    if (digit === 5) {
      // Volver a la lista
      session.viewingProduct = undefined;
      session.editingSessionField = undefined;
      await this.sendSessionProductList(jid, session);
      return;
    }
    if (digit === 4) {
      const product = session.products.find(p => p.position === session.viewingProduct);
      if (!product) return;
      try {
        await this.callBackend("/api/price/confirm", {
          observation_id: product.observation_id,
          user_id: jid,
          confirmed_has_discount: false,
        });
        product.has_discount = false;
        product.discounted_price = undefined;
        product.original_price = undefined;

        if (session.reviewingExpired) {
          // Remove from review list and renumber remaining
          session.products = session.products.filter(p => p.observation_id !== product.observation_id);
          session.products.forEach((p, i) => { p.position = i + 1; });
          session.viewingProduct = undefined;
          if (session.products.length === 0) {
            this.activeSessions.delete(jid);
            await this.safeReply(jid, "✅ Todos los descuentos verificados. ¡Hasta luego!");
          } else {
            await this.sendSessionProductList(jid, session);
          }
        } else {
          await this.sendProductDetailView(jid, session, product);
        }
      } catch (err: any) {
        await this.safeReply(jid, `❌ Error: ${err?.message}`);
      }
      return;
    }

    const fieldMap: Record<number, SessionEditField> = { 1: "nombre", 2: "precio", 3: "descuento" };
    const field = fieldMap[digit];
    if (!field) return;

    session.editingSessionField = field;
    const prompts: Record<string, string> = {
      nombre: "¿Cuál es el nombre?",
      precio: "¿Cuál es el precio?",
      descuento: "¿Cuál es el precio con descuento?",
    };
    await this.safeReply(jid, prompts[field]);
  }

  private async handleSessionFieldInput(jid: string, text: string, session: ActiveSession): Promise<void> {
    const field = session.editingSessionField!;
    session.editingSessionField = null;

    const product = session.products.find(p => p.position === session.viewingProduct);
    if (!product) {
      session.viewingProduct = undefined;
      await this.sendSessionProductList(jid, session);
      return;
    }

    const t = text.trim();
    const payload: Record<string, any> = { observation_id: product.observation_id, user_id: jid };

    switch (field) {
      case "nombre": {
        if (t.length < 2) {
          await this.safeReply(jid, "❌ Nombre muy corto.");
          session.editingSessionField = field;
          return;
        }
        const name = t.toUpperCase();
        payload.confirmed_product = name;
        product.product_normalized = name;
        product.product_raw = name;
        break;
      }
      case "precio": {
        const price = parseInt(t.replace(/[^\d]/g, ""), 10);
        if (isNaN(price) || price < 100) {
          await this.safeReply(jid, "❌ Precio inválido.");
          session.editingSessionField = field;
          return;
        }
        payload.confirmed_price = price;
        product.price = price;
        break;
      }
      case "descuento": {
        const dp = parseInt(t.replace(/[^\d]/g, ""), 10);
        if (isNaN(dp) || dp < 100) {
          await this.safeReply(jid, "❌ Precio inválido.");
          session.editingSessionField = field;
          return;
        }
        payload.confirmed_has_discount = true;
        payload.confirmed_discounted_price = dp;
        product.has_discount = true;
        product.discounted_price = dp;
        if (!product.original_price && product.price) {
          product.original_price = product.price;
        }
        break;
      }
    }

    try {
      await this.callBackend("/api/price/confirm", payload);
      await this.sendProductDetailView(jid, session, product);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }


  private async handleSessionEnd(jid: string) {
    const session = this.activeSessions.get(jid);
    if (!session) {
      await this.safeReply(jid, "No tienes una sesión activa.");
      return;
    }

    try {
      const result = await this.callBackend("/api/session/end", {
        session_id: session.session_id,
        user_id: jid,
      }) as SessionEndResponse;

      const summary = `✅ *Sesión ${session.store.toUpperCase()} cerrada.*\nActualizaste ${result.updated_count} de ${result.total_count} productos.`;
      await this.safeReply(jid, summary);

      // If there are products with expired/unverified discounts → enter review mode
      if (result.expired_products && result.expired_products.length > 0) {
        const reviewSession: ActiveSession = {
          session_id: session.session_id,
          store: session.store,
          products: result.expired_products,
          reviewingExpired: true,
        };
        this.activeSessions.set(jid, reviewSession);
        await this.sendSessionProductList(jid, reviewSession);
      } else {
        this.activeSessions.delete(jid);
      }
    } catch (err: any) {
      this.activeSessions.delete(jid);
      await this.safeReply(jid, `✅ Sesión cerrada.`);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // BARCODE CONFLICT RESOLUTION
  // ═══════════════════════════════════════════════════════════════

  private async sendBarcodeSelectionMenu(jid: string, pending: PendingConfirmation): Promise<void> {
    const d = pending.currentData;
    const [bLlm, bOcr] = pending.barcodeOptions ?? [];
    let msg = `🔢 *Dos códigos detectados — ¿Cuál es el correcto?*\n\n`;
    msg += `1️⃣  \`${bLlm}\` — IA\n`;
    msg += `2️⃣  \`${bOcr}\` — Escáner\n`;
    msg += `3️⃣  Ingresar manualmente\n\n`;
    msg += `📦 ${d.product_raw}\n`;
    if (d.has_discount && d.discounted_price) {
      msg += `💲 ${formatPrice(d.original_price ?? d.price)} → ⚡ ${formatPrice(d.discounted_price)}`;
    } else {
      msg += `💲 ${formatPrice(d.price)}`;
    }
    await this.safeReply(jid, msg);
  }

  private async handleBarcodeSelection(jid: string, digit: number, pending: PendingConfirmation): Promise<void> {
    const [bLlm, bOcr] = pending.barcodeOptions ?? [];

    pending.awaitingBarcodeSelection = false;
    pending.barcodeOptions = undefined;

    if (digit === 1 || digit === 2) {
      const chosen = digit === 1 ? bLlm : bOcr;
      pending.currentData.barcode = chosen ?? undefined;
      if (chosen) {
        await this.patchConfirmation(jid, pending, { confirmed_barcode: chosen });
      }
      await this.sendEditMenu(jid, pending);
    } else {
      // Opción 3: ingresar manualmente — reutiliza el flujo de edición de barcode
      pending.editingField = "barcode";
      const key = await this.safeReplyWithKey(jid, this.buildPromptForField(pending));
      pending.lastPromptMsgKey = key;
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // IMAGE MESSAGE
  // ═══════════════════════════════════════════════════════════════

  private async handleImageMessage(jid: string, msg: any, caption: string) {
    const mime_type = msg.message.imageMessage.mimetype || "image/jpeg";
    const ext = mime_type.split("/")[1] || "jpg";
    const filename = join(tmpdir(), `img-${Date.now()}.${ext}`);

    try {
      const stream = await downloadMediaMessage(
        msg, "stream", {},
        { logger: P({ level: "silent" }), reuploadRequest: this.sock.updateMediaMessage }
      );
      await this.downloadAndSaveMedia(stream, filename);

      // Store from caption or active session
      const captionStore = extractStore(caption);
      const store = captionStore !== "desconocido" ? captionStore : undefined;

      const payload = {
        user_id: jid,
        store,
        message: caption || null,
        mime_type,
        file_base64: fileToBase64(filename),
      };

      const result: PriceExtractResponse = await this.callBackend("/api/price/extract", payload);

      // Always show for editing — user can confirm, correct, or discard
      if (result.product_raw || result.price || result.barcode) {
        const pending: PendingConfirmation = {
          observation_id: result.observation_id,
          fields: result.confirmation_fields,
          extracted: result,
          timestamp: Date.now(),
          currentData: extractableToEditable(result),
          editingField: null,
        };
        // If both sources found different barcodes, ask user to pick first
        if (result.barcode_conflict && result.barcode_llm && result.barcode_ocr) {
          pending.awaitingBarcodeSelection = true;
          pending.barcodeOptions = [result.barcode_llm, result.barcode_ocr];
        }
        this.pendingConfirmations.set(jid, pending);
        if (pending.awaitingBarcodeSelection) {
          await this.sendBarcodeSelectionMenu(jid, pending);
        } else {
          await this.sendEditMenu(jid, pending);
        }
      } else {
        await this.safeReply(jid, this.buildEditableResultMessage(result));
      }
    } finally {
      if (filename && existsSync(filename)) {
        try { unlinkSync(filename); } catch {}
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // TEXT MESSAGE
  // ═══════════════════════════════════════════════════════════════

  private async handleTextMessage(jid: string, text: string) {
    // 0. Pending confirmation intercepts (before session/store checks)
    const pendingEdit = this.pendingConfirmations.get(jid);
    if (pendingEdit) {
      // Barcode conflict resolution (takes priority over everything else)
      if (pendingEdit.awaitingBarcodeSelection) {
        const dm = text.trim().match(/^([123])$/);
        if (dm) {
          await this.handleBarcodeSelection(jid, parseInt(dm[1], 10), pendingEdit);
        } else {
          await this.safeReply(jid, "Escribe *1*, *2* o *3* para elegir el código.");
        }
        return;
      }
      // Waiting for a typed value for a specific field
      if (pendingEdit.editingField !== null) {
        await this.handleEditStep(jid, text);
        return;
      }
      // Digit selection from numbered menu
      const digitMatch = text.trim().match(/^([1-9])$/);
      if (digitMatch) {
        await this.handleTextMenuDigit(jid, parseInt(digitMatch[1], 10), pendingEdit);
        return;
      }
    }

    // 0. Shopping list mode: digit → delete item or clear all
    if (this.shoppingListMode.has(jid) && !this.activeSessions.has(jid)) {
      const listItems = this.shoppingListMode.get(jid)!;
      const dList = text.trim().match(/^(\d{1,2})$/);
      if (dList) {
        const num = parseInt(dList[1], 10);
        const clearNum = listItems.length + 1;
        if (num === clearNum) {
          // "Limpiar toda la lista" option selected
          this.shoppingListMode.delete(jid);
          await this.handleListClear(jid);
          return;
        }
        if (num >= 1 && num <= listItems.length) {
          const removed = listItems[num - 1];
          try {
            await this.callBackend("/api/shopping/list/remove", { user_id: jid, product: removed });
          } catch (_) { /* ignore removal errors */ }
          listItems.splice(num - 1, 1);
          if (listItems.length === 0) {
            this.shoppingListMode.delete(jid);
            await this.safeReply(jid, "✅ Lista vacía.");
          } else {
            // Refresh the list with updated items
            this.shoppingListMode.delete(jid);
            await this.handleListGet(jid);
          }
          return;
        }
      }
      // Non-digit or out-of-range → exit list mode, fall through to normal intent
      this.shoppingListMode.delete(jid);
    }

    // 1. Active session flow
    const session = this.activeSessions.get(jid);
    if (session) {
      // Waiting for a field value
      if (session.editingSessionField) {
        await this.handleSessionFieldInput(jid, text, session);
        return;
      }
      // "listo" in review mode → close without backend call
      if (session.reviewingExpired && /^(listo|fin|terminé|termine|sali|salí|cerrar)$/i.test(text.trim())) {
        this.activeSessions.delete(jid);
        await this.safeReply(jid, "👍 Revisión finalizada.");
        return;
      }
      // In product detail view: digits 1-4 map to edit actions
      if (session.viewingProduct !== undefined) {
        const d = text.trim().match(/^([1-5])$/);
        if (d) {
          await this.handleProductDetailDigit(jid, session, parseInt(d[1], 10));
          return;
        }
      } else {
        // In list view: digit selects a product
        const d = text.trim().match(/^(\d{1,2})$/);
        if (d) {
          const num = parseInt(d[1], 10);
          const product = session.products.find(p => p.position === num);
          if (product) {
            session.viewingProduct = num;
            await this.sendProductDetailView(jid, session, product);
            return;
          }
        }
      }
    }

    // 2. Check store arrival
    const arrivalStore = detectStoreArrival(text);
    if (arrivalStore) {
      await this.handleStoreArrival(jid, arrivalStore);
      return;
    }

    const { intent, query } = parseIntent(text);

    switch (intent) {
      case "session_end":
        await this.handleSessionEnd(jid);
        break;

      case "ver_productos":
        await this.handleVerProductos(jid);
        break;

      case "shopping_recommend":
        await this.handleShoppingRecommend(jid);
        break;

      case "list_add":
        await this.handleListAdd(jid, query);
        break;

      case "list_remove":
        await this.handleListRemove(jid, query);
        break;

      case "list_get":
        await this.handleListGet(jid);
        break;

      case "list_clear":
        await this.handleListClear(jid);
        break;

      case "help":
        await this.safeReply(jid, this.getHelpMessage());
        break;

      case "search":
        await this.handleSearch(jid, query);
        break;

      case "compare":
        await this.handleCompare(jid, query);
        break;

      case "cheapest":
        await this.handleCheapest(jid, query);
        break;

      case "confirm_yes":
        await this.handleConfirmYes(jid);
        break;

      case "confirm_no":
        await this.handleConfirmNo(jid);
        break;

      case "confirm_price":
        await this.handleConfirmPrice(jid, query);
        break;

      case "edit_price":
        await this.handleEditPrice(jid, query);
        break;

      case "edit_product":
        await this.handleEditProduct(jid, query);
        break;

      case "edit_both":
        await this.handleEditBoth(jid, query);
        break;

      case "edit_barcode":
        await this.handleEditField(jid, { confirmed_barcode: query });
        await this.safeReply(jid, `✅ Código corregido a \`${query}\` y guardado en catálogo.`);
        break;

      case "edit_store":
        await this.handleEditField(jid, { confirmed_store: query.toLowerCase() });
        await this.safeReply(jid, `✅ Tienda corregida a *${query.toUpperCase()}*.`);
        break;

      case "edit_discount":
        await this.handleEditField(jid, {
          confirmed_has_discount: true,
          confirmed_discounted_price: parseInt(query, 10),
        });
        await this.safeReply(jid, `✅ Descuento registrado: precio con descuento ${formatPrice(parseInt(query, 10))}.`);
        break;

      case "edit_no_discount":
        await this.handleEditField(jid, { confirmed_has_discount: false });
        await this.safeReply(jid, `✅ Marcado sin descuento.`);
        break;

      case "edit_volume":
        await this.handleEditField(jid, { confirmed_volume_ml: parseInt(query, 10) });
        await this.safeReply(jid, `✅ Volumen corregido a ${query} ML.`);
        break;

      case "stats":
        await this.handleStats(jid);
        break;

      case "admin_clear":
        await this.handleAdminClear(jid);
        break;

      case "admin_catalog":
        await this.handleAdminCatalog(jid);
        break;

      default:
        if (this.pendingConfirmations.has(jid)) {
          await this.handleConfirmText(jid, text);
        } else if (session) {
          await this.safeReply(jid,
            `Sesión activa en *${session.store.toUpperCase()}*.\n` +
            `Escribe un número para ver un producto o *listo* para terminar.`
          );
        } else {
          await this.safeReply(jid, this.getHelpMessage());
        }
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // SHOPPING LIST HANDLERS
  // ═══════════════════════════════════════════════════════════════

  private async handleShoppingRecommend(jid: string) {
    try {
      const url = `/api/shopping/recommend?user_id=${encodeURIComponent(jid)}`;
      const result = await this.callBackend(url, null, "GET");

      if (!result.items || result.items.length === 0) {
        await this.safeReply(jid,
          `❌ Tu lista de compras está vacía o no hay precios en la BD.\n` +
          `Agrega productos con: *agregar leche, arroz, aceite*`
        );
        return;
      }

      let msg = `🛒 *Lista de compras optimizada:*\n`;

      // Group by store
      const grouped: Record<string, any[]> = result.grouped_by_store || {};
      for (const [store, items] of Object.entries(grouped)) {
        msg += `\n🏪 *${store.toUpperCase()}*\n`;
        for (const item of items as any[]) {
          if (item.has_discount && item.discounted_price) {
            msg += `  • ${item.product}: ${formatPrice(item.discounted_price)} ⚡ (antes ${formatPrice(item.price)})\n`;
          } else {
            msg += `  • ${item.product}: ${formatPrice(item.effective_price)}\n`;
          }
          if (item.discount_unverified) {
            msg += `    ⚠️ Verifica si el descuento sigue vigente\n`;
          }
        }
      }

      if (result.not_found && result.not_found.length > 0) {
        msg += `\n❓ *Sin precio en BD:*\n`;
        for (const p of result.not_found) msg += `  • ${p}\n`;
      }

      if (result.total_estimated > 0) {
        msg += `\n💰 *Total estimado: ${formatPrice(result.total_estimated)}*`;
      }

      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleListAdd(jid: string, query: string) {
    if (!query) {
      await this.safeReply(jid, "¿Qué productos quieres agregar?\nEjemplo: *agregar leche, arroz, aceite*");
      return;
    }

    const products = query.split(/[,;]+/).map(p => p.trim()).filter(Boolean);

    try {
      const result = await this.callBackend("/api/shopping/list/add", {
        user_id: jid,
        products,
      });
      await this.safeReply(jid, `✅ Agregué ${products.length} producto(s) a tu lista:\n${products.map(p => `• ${p}`).join("\n")}`);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleListRemove(jid: string, query: string) {
    if (!query) {
      await this.safeReply(jid, "¿Qué producto quieres quitar? Ejemplo: *quitar leche*");
      return;
    }

    try {
      await this.callBackend("/api/shopping/list/remove", { user_id: jid, product: query });
      await this.safeReply(jid, `✅ Eliminé *${query}* de tu lista.`);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleListGet(jid: string) {
    try {
      const url = `/api/shopping/list?user_id=${encodeURIComponent(jid)}`;
      const result = await this.callBackend(url, null, "GET");

      if (!result.products || result.products.length === 0) {
        await this.safeReply(jid,
          `Tu lista de compras está vacía.\n` +
          `Agrega con: *agregar leche, arroz, aceite*`
        );
        return;
      }

      const items = result.products as string[];
      // Enter shopping list edit mode so digits delete items
      this.shoppingListMode.set(jid, [...items]);

      const clearNum = items.length + 1;
      let msg = `📋 *Tu lista de compras — ${result.count} item${result.count !== 1 ? "s" : ""}*\n\n`;
      items.forEach((p: string, i: number) => {
        msg += `${numEmoji(i + 1)} ${p.toUpperCase()}\n`;
      });
      msg += `${numEmoji(clearNum)} 🗑️ Limpiar toda la lista\n`;
      msg += `\nEscribe un número para eliminar.\n`;
      msg += `*quiero mercar* para ver precios.`;
      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleListClear(jid: string) {
    try {
      await this.callBackend("/api/shopping/list/clear", { user_id: jid });
      this.shoppingListMode.delete(jid);
      await this.safeReply(jid, "✅ Lista de compras limpiada.");
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleVerProductos(jid: string): Promise<void> {
    const session = this.activeSessions.get(jid);
    try {
      // Always fetch fresh data from DB — the in-memory session.products is stale
      // (it was loaded at session start and doesn't include products scanned since then)
      const result = await this.callBackend(
        `/api/price/all-products?user_id=${encodeURIComponent(jid)}`,
        null, "GET"
      );
      let grouped: Record<string, any[]> = (result?.stores as Record<string, any[]>) ?? {};

      // When inside a session, filter to show only that store
      if (session && Object.keys(grouped).length > 1) {
        const storeData = grouped[session.store];
        if (storeData) grouped = { [session.store]: storeData };
      }

      if (!Object.keys(grouped).length) {
        await this.safeReply(jid, "No tienes productos registrados aún.\nEnvía fotos de etiquetas para empezar.");
        return;
      }

      let totalCount = 0;
      for (const prods of Object.values(grouped)) totalCount += prods.length;
      let msg = `📦 *Catálogo — ${totalCount} producto${totalCount !== 1 ? "s" : ""}*\n`;

      for (const [store, products] of Object.entries(grouped)) {
        msg += `\n🏪 *${store.toUpperCase()}*\n`;
        msg += `──────────────────\n`;
        products.forEach((p: any, idx: number) => {
          const name = (p.product_normalized ?? p.product_raw ?? "?").toUpperCase();
          msg += `\n${numEmoji(idx + 1)} *${name}*\n`;
          if (p.barcode) msg += `   🔢 ${p.barcode}\n`;
          if (p.has_discount && p.discounted_price) {
            const regular = p.original_price && p.original_price !== p.discounted_price
              ? p.original_price : null;
            if (regular) {
              msg += `   💲 ${formatPrice(regular)} → ⚡ ${formatPrice(p.discounted_price)}\n`;
            } else {
              msg += `   💲 ${formatPrice(p.discounted_price)} ⚡\n`;
            }
            if (p.promo_end_date) msg += `   🗓 válido hasta: ${p.promo_end_date}\n`;
          } else {
            msg += `   💲 ${formatPrice(p.price)}\n`;
          }
        });
      }

      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // SEARCH / COMPARE HANDLERS
  // ═══════════════════════════════════════════════════════════════

  private async handleSearch(jid: string, query: string) {
    if (!query) {
      await this.safeReply(jid, "🔍 ¿Qué producto quieres buscar?\nEjemplo: *buscar leche alquería*");
      return;
    }
    try {
      const result = await this.callBackend("/api/price/search", { user_id: jid, query, limit: 5 });

      if (!result.results?.length) {
        await this.safeReply(jid, `❌ No encontré "${query}" en la base de datos.`);
        return;
      }

      let msg = `🔍 *Resultados para "${query}":*\n\n`;
      for (const r of result.results) {
        msg += `📦 ${r.product_name}\n`;
        if (r.has_discount && r.discounted_price) {
          msg += `   🏪 ${r.store}: ${formatPrice(r.discounted_price)} ⚡ (antes ${formatPrice(r.price)})\n\n`;
        } else {
          msg += `   🏪 ${r.store}: ${formatPrice(r.price)}\n\n`;
        }
      }
      if (result.cheapest_store) {
        msg += `💰 *Más barato:* ${result.cheapest_store} (${formatPrice(result.cheapest_price)})`;
      }
      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error buscando: ${err?.message}`);
    }
  }

  private async handleCompare(jid: string, query: string) {
    if (!query) {
      await this.safeReply(jid, "📊 Ejemplo: *comparar leche alquería*");
      return;
    }
    try {
      const result = await this.callBackend("/api/price/compare", { user_id: jid, product_name: query });

      if (!result.stores?.length) {
        await this.safeReply(jid, `❌ No encontré "${query}" en múltiples tiendas.`);
        return;
      }

      let msg = `📊 *Comparación: ${result.product_name}*\n\n`;
      for (const s of result.stores) {
        const isCheapest = s.store === result.cheapest?.store;
        const emoji = isCheapest ? "✅" : "🏪";
        if (s.has_discount && s.discounted_price) {
          msg += `${emoji} ${s.store}: ${formatPrice(s.discounted_price)} ⚡ (antes ${formatPrice(s.price)})\n`;
        } else {
          msg += `${emoji} ${s.store}: ${formatPrice(s.price)}\n`;
        }
      }
      if (result.savings && result.savings > 0) {
        msg += `\n💰 *Ahorras ${formatPrice(result.savings)}* comprando en ${result.cheapest?.store}`;
      }
      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error comparando: ${err?.message}`);
    }
  }

  private async handleCheapest(jid: string, query: string) {
    if (!query) {
      await this.safeReply(jid, "💰 Ejemplo: *más barato leche*");
      return;
    }
    try {
      const url = `/api/price/cheapest?product=${encodeURIComponent(query)}`;
      const result = await this.callBackend(url, null, "GET");

      if (!result.found) {
        await this.safeReply(jid, `❌ No encontré "${query}" en la base de datos.`);
        return;
      }

      let msg = `💰 *Mejor precio:*\n\n📦 ${result.product}\n🏪 ${result.store}\n`;
      if (result.has_discount && result.discounted_price) {
        msg += `💲 ${formatPrice(result.discounted_price)} ⚡ (antes ${formatPrice(result.price)})`;
      } else {
        msg += `💲 ${formatPrice(result.price)}`;
      }
      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // CONFIRMATION HANDLERS
  // ═══════════════════════════════════════════════════════════════

  /** Generic field patcher — sends only the provided fields to /confirm */
  private async handleEditField(jid: string, fields: Record<string, any>) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta."); return; }
    try {
      await this.callBackend("/api/price/confirm", {
        observation_id: pending.observation_id,
        user_id: jid,
        confirmed_barcode: pending.extracted.barcode,
        ...fields,
      });
      // Keep pending so user can continue editing other fields
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleEditPrice(jid: string, priceStr: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta."); return; }
    const price = parseInt(priceStr, 10);
    if (isNaN(price) || price < 100) {
      await this.safeReply(jid, "❌ Precio inválido. Ejemplo: *precio 43100*"); return;
    }
    await this.handleEditField(jid, { confirmed_price: price });
    await this.safeReply(jid, `✅ Precio → ${formatPrice(price)}. Sigue corrigiendo o di *si* para guardar.`);
  }

  private async handleEditProduct(jid: string, productName: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta."); return; }
    if (!productName || productName.length < 2) {
      await this.safeReply(jid, "❌ Nombre muy corto. Ejemplo: *producto Alquería Sixpack 1800ml*"); return;
    }
    const name = productName.toUpperCase();
    await this.handleEditField(jid, { confirmed_product: name });
    await this.safeReply(jid, `✅ Producto → "${name}" 📚. Sigue corrigiendo o di *si*.`);
  }

  private async handleEditBoth(jid: string, query: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta."); return; }
    const [priceStr, productName] = query.split("|");
    const price = parseInt(priceStr, 10);
    if (isNaN(price) || price < 100 || !productName) {
      await this.safeReply(jid, "❌ Formato: *precio 43100 producto Alquería Sixpack*"); return;
    }
    const name = productName.toUpperCase();
    await this.handleEditField(jid, { confirmed_price: price, confirmed_product: name });
    await this.safeReply(jid, `✅ Precio → ${formatPrice(price)} | Producto → "${name}" 📚. Di *si* para guardar.`);
  }

  private async handleConfirmYes(jid: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente por confirmar."); return; }
    try {
      await this.callBackend("/api/price/confirm", {
        observation_id: pending.observation_id,
        user_id: jid,
        confirmed_barcode: pending.extracted.barcode,
      });
      this.pendingConfirmations.delete(jid);
      const barcode = pending.extracted.barcode;
      await this.safeReply(jid,
        `✅ *Guardado.*` +
        (barcode ? `\n📚 Código ${barcode} en catálogo para próximas fotos.` : "")
      );
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error confirmando: ${err?.message}`);
    }
  }

  private async handleConfirmNo(jid: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente."); return; }
    this.pendingConfirmations.delete(jid);
    await this.safeReply(jid, "❌ Cancelado. Puedes enviar otra foto.");
  }

  private async handleConfirmPrice(jid: string, priceStr: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) { await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta."); return; }
    const price = parseInt(priceStr, 10);
    if (isNaN(price) || price < 100) {
      await this.safeReply(jid, "❌ Precio inválido. Escribe el precio sin puntos.\nEjemplo: *35350*");
      return;
    }
    try {
      await this.callBackend("/api/price/confirm", { observation_id: pending.observation_id, user_id: jid, confirmed_price: price });
      this.pendingConfirmations.delete(jid);
      await this.safeReply(jid, `✅ Precio corregido a ${formatPrice(price)} y guardado.`);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleConfirmText(jid: string, text: string) {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending || !pending.fields.includes("product") || text.length <= 3) return;
    const name = text.toUpperCase();
    try {
      await this.callBackend("/api/price/confirm", { observation_id: pending.observation_id, user_id: jid, confirmed_product: name });
      this.pendingConfirmations.delete(jid);
      await this.safeReply(jid, `✅ Producto actualizado a "${name}" y guardado.`);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleStats(jid: string) {
    await this.safeReply(jid, "📊 *Estadísticas*\n\nFunción próximamente.");
  }

  private async handleAdminClear(jid: string) {
    try {
      await this.callBackend("/api/admin/clear", {}, "DELETE");
      this.pendingConfirmations.clear();
      this.activeSessions.clear();
      await this.safeReply(jid, "🗑️ *Todos los registros eliminados.*\nLa BD está limpia para volver a probar.");
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  private async handleAdminCatalog(jid: string) {
    try {
      const result = await this.callBackend("/api/admin/catalog", null, "GET");
      if (!result.count) {
        await this.safeReply(jid, "📚 El catálogo está vacío.\nGuarda productos enviando fotos y confirmando con *si*.");
        return;
      }

      const limit = 20;
      const entries = result.catalog.slice(0, limit);
      let msg = `📚 *Catálogo — ${result.count} producto${result.count !== 1 ? "s" : ""}*\n`;
      msg += `─────────────────────\n`;

      entries.forEach((e: any, i: number) => {
        const store = (e.store || "todas").toUpperCase();
        const vol = e.volume_ml ? ` · ${e.volume_ml} ML` : "";

        // Price line
        let priceLine = "💲 Sin precio aún";
        if (e.price) {
          if (e.has_discount && e.discounted_price) {
            priceLine = `💲 $${e.price.toLocaleString("es-CO")} → ⚡ $${e.discounted_price.toLocaleString("es-CO")}`;
          } else {
            priceLine = `💲 $${e.price.toLocaleString("es-CO")}`;
          }
        }

        // Last seen date (only date, not time)
        const lastSeen = e.last_seen
          ? `· visto ${e.last_seen.slice(0, 10)}`
          : "";

        msg += `\n*${i + 1}. ${e.product_name}${vol}*\n`;
        msg += `   🏪 ${store} · 🔢 ${e.barcode}\n`;
        msg += `   ${priceLine} ${lastSeen}\n`;
      });

      if (result.count > limit) {
        msg += `\n─────────────────────\n`;
        msg += `_...y ${result.count - limit} más. Máx ${limit} mostrados._`;
      }

      await this.safeReply(jid, msg);
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error: ${err?.message}`);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // INTERACTIVE MENU (numbered text)
  // ═══════════════════════════════════════════════════════════════

  private async sendEditMenu(jid: string, pending: PendingConfirmation): Promise<void> {
    const d = pending.currentData;
    const fromCatalog = pending.extracted.product_source === "catalog";
    const needsPrice = !d.price || pending.fields.includes("price");

    let msg = `🛒 *${d.store.toUpperCase()} · ${d.product_raw}${fromCatalog ? " 📚" : ""}*\n`;
    if (d.has_discount && d.discounted_price) {
      msg += `💲 ${formatPrice(d.original_price ?? d.price)} → ⚡ ${formatPrice(d.discounted_price)}\n`;
      if (d.promo_end_date) msg += `🗓 Válido hasta: ${d.promo_end_date}\n`;
    } else {
      msg += `💲 ${needsPrice ? "⚠️ Sin precio" : formatPrice(d.price)}\n`;
    }
    if (d.barcode) msg += `🔢 ${d.barcode}\n`;

    msg += `\n*¿Qué corrijo?*\n`;
    msg += `1️⃣  ✅ Guardar así\n`;
    msg += `2️⃣  💲 Precio\n`;
    if (d.has_discount) {
      msg += `3️⃣  ⚡ Descuento\n`;
      msg += `4️⃣  🚫 Sin descuento\n`;
      msg += `5️⃣  🗓 Fecha promo\n`;
      msg += `6️⃣  📦 Nombre\n`;
      msg += `7️⃣  🔢 Código\n`;
      msg += `8️⃣  ❌ Descartar\n`;
    } else {
      msg += `3️⃣  ⚡ Agregar descuento\n`;
      msg += `4️⃣  📦 Nombre\n`;
      msg += `5️⃣  🔢 Código\n`;
      msg += `6️⃣  ❌ Descartar\n`;
    }
    await this.safeReply(jid, msg);
  }

  private async handleTextMenuDigit(jid: string, digit: number, pending: PendingConfirmation): Promise<void> {
    const withDiscount = pending.currentData.has_discount;
    const actionMap: Record<number, string> = withDiscount
      ? { 1: "save", 2: "price", 3: "discount", 4: "no_discount", 5: "promo_date", 6: "product", 7: "barcode", 8: "discard" }
      : { 1: "save", 2: "price", 3: "discount", 4: "product", 5: "barcode", 6: "discard" };

    const action = actionMap[digit];
    if (!action) return;

    if (action === "save") {
      await this.handleConfirmYesFromMenu(jid, pending);
    } else if (action === "discard") {
      this.pendingConfirmations.delete(jid);
      await this.safeReply(jid, "❌ Cancelado. Puedes enviar otra foto.");
    } else if (action === "no_discount") {
      pending.currentData.has_discount = false;
      pending.currentData.discounted_price = undefined;
      pending.currentData.original_price = undefined;
      await this.sendEditMenu(jid, pending);
    } else {
      const field = action as EditingField;
      pending.editingField = field;
      const key = await this.safeReplyWithKey(jid, this.buildPromptForField(pending));
      pending.lastPromptMsgKey = key;
    }
  }

  /** Returns the question text to show when entering edit mode for a field */
  private buildPromptForField(pending: PendingConfirmation): string {
    const d = pending.currentData;
    switch (pending.editingField) {
      case "price":
        return `💲 *¿Cuál es el precio?*\nActual: ${formatPrice(d.price)}\nEscribe solo el número, ej: *43100*`;
      case "discount":
        return `⚡ *¿Cuál es el precio final con descuento?*\nActual: ${formatPrice(d.discounted_price)}\nEscribe solo el número, ej: *38000*`;
      case "product":
        return `📦 *¿Cómo se llama el producto?*\nActual: *${d.product_raw}*\nEscribe el nombre completo`;
      case "barcode":
        return `🔢 *¿Cuál es el código de barras?*\nActual: ${d.barcode ?? "no detectado"}\nEscribe los 8-14 dígitos`;
      case "store":
        return `🏪 *¿En qué tienda estás?*\nActual: *${d.store.toUpperCase()}*\nEscribe el nombre, ej: *exito*`;
      case "promo_date":
        return `🗓 *¿Fecha de fin de promo?*\nActual: ${d.promo_end_date ?? "no registrada"}\nFormato DD/MM/YYYY, ej: *30/05/2026*`;
      default:
        return "¿Cuál es el valor?";
    }
  }

  /** Like safeReply but returns the sent message key (needed to delete the prompt later) */
  private async safeReplyWithKey(
    jid: string,
    text: string
  ): Promise<{ remoteJid: string; fromMe: boolean; id: string }> {
    const sent = await this.sock.sendMessage(jid, { text });
    return { remoteJid: jid, fromMe: true, id: sent!.key.id! };
  }

  /** Thin wrapper over /api/price/confirm — always includes observation_id, user_id, barcode */
  private async patchConfirmation(
    jid: string,
    pending: PendingConfirmation,
    fields: Record<string, any>
  ): Promise<void> {
    try {
      await this.callBackend("/api/price/confirm", {
        observation_id: pending.observation_id,
        user_id: jid,
        confirmed_barcode: pending.currentData.barcode ?? pending.extracted.barcode,
        ...fields,
      });
    } catch (err: any) {
      console.error("[patchConfirmation]", err?.message);
      // Non-fatal: in-memory state already updated, user can still save
    }
  }

  /** Routes a rowId selected from the interactive list menu */
  private async handleListSelection(jid: string, rowId: string): Promise<void> {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending) {
      await this.safeReply(jid, "No hay nada pendiente. Envía una foto de etiqueta.");
      return;
    }

    switch (rowId) {
      case "save":
        await this.handleConfirmYesFromMenu(jid, pending);
        break;

      case "discard":
        this.pendingConfirmations.delete(jid);
        await this.safeReply(jid, "❌ Cancelado. Puedes enviar otra foto.");
        break;

      case "no_discount":
        pending.currentData.has_discount = false;
        pending.currentData.discounted_price = undefined;
        pending.currentData.original_price = undefined;
        await this.sendEditMenu(jid, pending);
        break;

      case "edit_price":
      case "edit_discount":
      case "edit_product":
      case "edit_barcode":
      case "edit_store": {
        const fieldMap: Record<string, EditingField> = {
          edit_price: "price",
          edit_discount: "discount",
          edit_product: "product",
          edit_barcode: "barcode",
          edit_store: "store",
        };
        pending.editingField = fieldMap[rowId];
        const key = await this.safeReplyWithKey(jid, this.buildPromptForField(pending));
        pending.lastPromptMsgKey = key;
        break;
      }

      default:
        await this.safeReply(jid, "Opción no reconocida.");
    }
  }

  /** Processes a typed value when editingField is set — validates, updates, shows menu again */
  private async handleEditStep(jid: string, text: string): Promise<void> {
    const pending = this.pendingConfirmations.get(jid);
    if (!pending || pending.editingField === null) return;

    const field = pending.editingField;
    pending.editingField = null; // reset state machine immediately
    pending.lastPromptMsgKey = undefined;

    const t = text.trim();

    const reEnter = async () => {
      pending.editingField = field;
      const key = await this.safeReplyWithKey(jid, this.buildPromptForField(pending));
      pending.lastPromptMsgKey = key;
    };

    switch (field) {
      case "price": {
        const price = parseInt(t.replace(/[^\d]/g, ""), 10);
        if (isNaN(price) || price < 100) {
          await this.safeReply(jid, "❌ Precio inválido. Escribe solo números, ej: *43100*");
          await reEnter(); return;
        }
        pending.currentData.price = price;
        break;
      }

      case "discount": {
        const dp = parseInt(t.replace(/[^\d]/g, ""), 10);
        if (isNaN(dp) || dp < 100) {
          await this.safeReply(jid, "❌ Precio inválido. Escribe solo números, ej: *38000*");
          await reEnter(); return;
        }
        pending.currentData.has_discount = true;
        pending.currentData.original_price = pending.currentData.price;
        pending.currentData.discounted_price = dp;
        break;
      }

      case "product": {
        if (t.length < 2) {
          await this.safeReply(jid, "❌ Nombre muy corto. Escribe el nombre completo del producto.");
          await reEnter(); return;
        }
        pending.currentData.product_raw = t.toUpperCase();
        break;
      }

      case "barcode": {
        if (!/^\d{8,14}$/.test(t)) {
          await this.safeReply(jid, "❌ Código inválido. Debe tener entre 8 y 14 dígitos.");
          await reEnter(); return;
        }
        pending.currentData.barcode = t;
        break;
      }

      case "store": {
        const canonical = STORE_ALIASES[t.toLowerCase()] ?? t.toLowerCase();
        pending.currentData.store = canonical;
        break;
      }

      case "promo_date": {
        if (!/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(t)) {
          await this.safeReply(jid, "❌ Formato inválido. Usa DD/MM/YYYY, ej: *30/05/2026*");
          await reEnter(); return;
        }
        pending.currentData.promo_end_date = t;
        break;
      }
    }

    // Show updated poll after each successful edit
    await this.sendEditMenu(jid, pending);
  }

  /** Confirm and save from the interactive menu — only HERE does the DB write happen */
  private async handleConfirmYesFromMenu(jid: string, pending: PendingConfirmation): Promise<void> {
    try {
      const d = pending.currentData;
      const result = await this.callBackend("/api/price/save", {
        user_id: jid,
        store: d.store,
        product_raw: d.product_raw,
        price: d.has_discount ? (d.original_price ?? d.price) : d.price,
        has_discount: d.has_discount,
        original_price: d.original_price,
        discounted_price: d.has_discount ? d.discounted_price : undefined,
        barcode: d.barcode,
        volume_ml: d.volume_ml,
        promo_end_date: d.promo_end_date,
        extraction_method: pending.extracted.extraction_method,
        ocr_confidence: pending.extracted.ocr_confidence,
      });
      this.pendingConfirmations.delete(jid);
      const barcode = d.barcode;
      await this.safeReply(
        jid,
        `✅ *Guardado.*` +
        (barcode ? `\n📚 Código ${barcode} en catálogo para próximas fotos.` : "")
      );
    } catch (err: any) {
      await this.safeReply(jid, `❌ Error guardando: ${err?.message}`);
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // MESSAGE BUILDERS
  // ═══════════════════════════════════════════════════════════════

  private buildEditableResultMessage(result: PriceExtractResponse): string {
    const fromCatalog = result.product_source === "catalog" || result.extraction_method === "catalog";

    // ── Catalog shortcut: product known, price NOT detected → ask for it ──
    if (fromCatalog && !result.price) {
      let msg = `📚 *Producto en catálogo — ¿Cuánto cuesta hoy?*\n\n`;
      msg += `🏪 ${result.store.toUpperCase()}\n`;
      msg += `📦 ${result.product_raw} 📚\n`;
      if (result.barcode) msg += `🔢 ${result.barcode}\n`;
      if (result.volume_ml) msg += `🥤 ${result.volume_ml} ML\n`;
      msg += `\nResponde con el precio:\n`;
      msg += `• *precio 43100* → precio normal\n`;
      msg += `• *descuento 38000* → tiene descuento (precio final)\n`;
      msg += `• *no* → cancelar`;
      return msg;
    }

    // ── Catalog shortcut: product known, price detected → confirm update ──
    if (fromCatalog && result.price) {
      let msg = `📚 *Producto reconocido — ¿Actualizamos el precio?*\n\n`;
      msg += `🏪 ${result.store.toUpperCase()}\n`;
      msg += `📦 ${result.product_raw} 📚\n`;
      if (result.has_discount && result.discounted_price) {
        msg += `💲 Regular: ${formatPrice(result.original_price || result.price)} → ⚡ ${formatPrice(result.discounted_price)}\n`;
      } else {
        msg += `💲 Precio detectado hoy: ${formatPrice(result.price)}\n`;
      }
      if (result.barcode) msg += `🔢 ${result.barcode}\n`;
      if (result.volume_ml) msg += `🥤 ${result.volume_ml} ML\n`;
      msg += `\n• *si* → guardar este precio\n`;
      msg += `• *precio 45000* → el precio es otro\n`;
      msg += `• *descuento 38000* → tiene descuento\n`;
      msg += `• *no* → cancelar`;
      return msg;
    }

    // ── Full editable result (LLM / OCR, product NOT in catalog) ──────
    const method = result.extraction_method === "llm_vision" ? "IA" : "OCR";
    const productLabel = `${result.product_raw || "No detectado"} (${method})`;

    let msg = `🛒 *Precio registrado — ¿Correcto?*\n\n`;
    msg += `🏪 ${result.store.toUpperCase()}\n`;
    msg += `📦 ${productLabel}\n`;

    if (result.has_discount && result.discounted_price) {
      msg += `💲 Regular: ${formatPrice(result.original_price || result.price)} → ⚡ ${formatPrice(result.discounted_price)}\n`;
    } else {
      msg += `💲 Precio: ${formatPrice(result.price)}\n`;
    }

    if (result.barcode) msg += `🔢 Código: ${result.barcode}\n`;
    if (result.volume_ml) msg += `🥤 ${result.volume_ml} ML\n`;

    if (result.confirmation_fields.includes("price")) {
      msg += `\n⚠️ *Precio no claro* — corrígelo con: _precio 43100_\n`;
    }
    if (result.confirmation_fields.includes("product")) {
      msg += `\n⚠️ *Nombre no claro* — corrígelo con: _producto Alquería Sixpack_\n`;
    }

    msg += `\nEdita lo que esté mal y di *si* cuando esté correcto:\n`;
    msg += `• *precio 43100* → precio\n`;
    msg += `• *producto Alquería Sixpack* → nombre 📚\n`;
    msg += `• *precio 43100 producto Alquería* → ambos\n`;
    msg += `• *codigo 7702177022764* → código de barras\n`;
    msg += `• *tienda exito* → tienda\n`;
    msg += `• *descuento 38000* → precio con descuento\n`;
    msg += `• *sin descuento* → quitar descuento\n`;
    msg += `• *volumen 1800* → volumen en ML\n`;
    msg += `• *si* → confirmar | *no* → descartar`;
    return msg;
  }

  private buildSuccessMessage(result: PriceExtractResponse): string {
    const method = result.extraction_method === "llm_vision" ? "IA" : "OCR";
    let msg = `🛒 *Precio registrado* (${method})\n\n`;
    msg += `🏪 Tienda: ${result.store}\n`;
    msg += `📦 Producto: ${result.product_raw || "No detectado"}\n`;

    if (result.has_discount && result.discounted_price) {
      msg += `💲 Precio regular: ${formatPrice(result.original_price || result.price)}\n`;
      msg += `⚡ Con descuento: ${formatPrice(result.discounted_price)}\n`;
    } else {
      msg += `💲 Precio: ${formatPrice(result.price)}\n`;
    }

    if (result.unit_type) msg += `📏 Por ${result.unit_type}: ${result.unit_price?.toFixed(2) || "?"}\n`;
    if (result.quantity) msg += `📦 Cantidad: ${result.quantity} unidades\n`;
    if (result.volume_ml) msg += `🥤 Volumen: ${result.volume_ml} ML\n`;
    if (result.barcode) msg += `🔢 Código: ${result.barcode}\n`;

    msg += `\n✅ Guardado correctamente`;
    return msg;
  }

  private getHelpMessage(): string {
    return (
      `🛒 *Rastreador de Precios de Supermercados*\n\n` +
      `📍 *Llegar a una tienda:*\n` +
      `   _estoy en el exito_ / _llegué al d1_\n\n` +
      `📸 *Escanear etiqueta:*\n` +
      `   Envía foto → siempre se muestra para confirmar/editar\n\n` +
      `✏️ *Después de escanear:*\n` +
      `   _si_ → confirmar y guardar\n` +
      `   _precio 43100_ → corregir precio\n` +
      `   _producto Alquería Sixpack_ → corregir nombre (📚 catálogo)\n` +
      `   _precio 43100 producto Alquería_ → corregir ambos\n` +
      `   _no_ → descartar\n\n` +
      `🔢 *En sesión activa:*\n` +
      `   escribe un número → ver detalle del producto\n` +
      `   en detalle: 1 nombre · 2 precio · 3 descuento · 4 sin descuento\n` +
      `   _volver_ → lista de productos\n` +
      `   _listo_ → cerrar sesión\n\n` +
      `📦 *Todos los productos:*\n` +
      `   _ver productos_ → listado agrupado por tienda\n\n` +
      `📋 *Lista de compras:*\n` +
      `   _agregar leche, arroz_ → agregar\n` +
      `   _quitar leche_ → quitar\n` +
      `   _mi lista_ → ver lista\n` +
      `   _quiero mercar_ → recomendación\n\n` +
      `🔍 *Consultas:*\n` +
      `   _buscar leche_ / _comparar arroz_ / _más barato aceite_\n\n` +
      `🏪 *Tiendas:* exito, d1, ara, jumbo, olimpica, makro, carulla, or, euro, metro, surtimax, alkosto`
    );
  }

  // ═══════════════════════════════════════════════════════════════
  // MAIN MESSAGE HANDLER
  // ═══════════════════════════════════════════════════════════════

  async onMessagesUpsert(message_array: any) {
    for (const msg of message_array.messages) {
      if (!msg.message) continue;
      if (msg.key.fromMe) continue;

      const jid = msg.key.remoteJid;
      const messageType = getContentType(msg.message);
      const messageText =
        msg.message.imageMessage?.caption ||
        msg.message.conversation ||
        msg.message.extendedTextMessage?.text ||
        "";

      this.sock.readMessages([msg.key]);

      try {
        // List selection response (interactive menu)
        if (messageType === "listResponseMessage") {
          const rowId = msg.message.listResponseMessage?.singleSelectReply?.selectedRowId;
          if (rowId) await this.handleListSelection(jid, rowId);
          continue;
        }
        if (messageType === "imageMessage") {
          await this.handleImageMessage(jid, msg, messageText);
          continue;
        }
        if (messageType === "audioMessage") {
          await this.safeReply(jid, "🎙️ Por ahora solo proceso fotos de etiquetas.");
          continue;
        }
        await this.handleTextMessage(jid, messageText);
      } catch (err: any) {
        console.error("Error procesando mensaje:", err?.message || err);
        await this.safeReply(jid, `❌ Error: ${err?.message || "fallo desconocido"}`);
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════
  // CONNECTION HANDLERS
  // ═══════════════════════════════════════════════════════════════

  async onConnectionUpdateQR(qr: string) {
    this.qrAttempts++;
    if (this.qrAttempts > this.maxQrAttempts) {
      console.log("Demasiados intentos de QR. Cerrando...");
      await this.sock.logout();
      process.exit(1);
      return;
    }
    QRCode.toString(qr, { type: "terminal", small: true }, (err, url) => {
      if (err) return;
      console.log(url);
      console.log(`Escanea el QR (${this.qrAttempts}/${this.maxQrAttempts})`);
    });
  }

  async onConnectionUpdateClose(lastDisconnect: { error: any } | undefined) {
    const statusCode = (lastDisconnect?.error as any)?.output?.statusCode;
    const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
    if (shouldReconnect) {
      await this.initSocket();
    } else {
      this.deleteAuthFolder("auth_info_baileys");
      process.exit(0);
    }
  }

  async onConnectionUpdate(update: { connection?: string; lastDisconnect?: { error: any }; qr?: string }) {
    const { connection, lastDisconnect, qr } = update;
    if (qr) await this.onConnectionUpdateQR(qr);
    if (connection === "open") {
      console.log("Conectado a WhatsApp");
      this.qrAttempts = 0;
      if (!this.promoCheckStarted) {
        this.promoCheckStarted = true;
        this.scheduleDailyPromoCheck();
      }
    }
    if (connection === "close") await this.onConnectionUpdateClose(lastDisconnect);
  }

  private scheduleDailyPromoCheck(): void {
    // Avoid importing node-cron at module level to keep it optional
    try {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      const cron = require("node-cron");
      cron.schedule("0 6 * * *", async () => {
        console.log("[CRON] Chequeando promos vencidas...");
        try {
          const data = await this.callBackend("/api/price/expired-promos", null, "GET") as {
            expired: Array<{ id: number; user_id: string; product_raw: string; store: string }>;
          };
          for (const item of data.expired || []) {
            try {
              await this.callBackend("/api/price/confirm", {
                observation_id: item.id,
                user_id: item.user_id,
                confirmed_has_discount: false,
              });
              if (item.user_id) {
                await this.sock.sendMessage(item.user_id, {
                  text: `⚠️ Promo vencida: *${item.product_raw}* en ${item.store.toUpperCase()} — descuento eliminado.`,
                });
              }
            } catch (innerErr: any) {
              console.error(`[CRON] Error procesando obs ${item.id}:`, innerErr?.message);
            }
          }
          if (data.expired?.length) {
            console.log(`[CRON] ${data.expired.length} promo(s) vencidas limpiadas.`);
          }
        } catch (err: any) {
          console.error("[CRON] Error chequeando promos:", err?.message);
        }
      }, { timezone: "America/Bogota" });
      console.log("[CRON] Tarea diaria 6 AM programada.");
    } catch {
      console.warn("[CRON] node-cron no instalado — tarea diaria no programada.");
    }
  }

  deleteAuthFolder(folderName: string) {
    const fullPath = join(process.cwd(), folderName);
    if (existsSync(fullPath)) {
      rmSync(fullPath, { recursive: true, force: true });
    }
  }
}

export { WhatsAppHandler };
