# 🛒 Supermarket Price Tracker

Sistema para rastrear y comparar precios de supermercados mediante WhatsApp.

## 📋 Características

- **📸 OCR de etiquetas**: Envía una foto de la etiqueta del precio y extrae automáticamente:
  - Precio principal
  - Precio por unidad (ML, g, kg)
  - Código de barras (EAN-13)
  - PLU
  - Cantidad y volumen
  - Detección de promociones (etiquetas rojas)

- **🔍 Consultas inteligentes**:
  - `buscar leche` - Buscar productos
  - `comparar leche alquería` - Comparar precios entre tiendas
  - `más barato leche` - Encontrar el mejor precio

- **✅ Confirmación de datos**: Si el OCR no está seguro, pide confirmación al usuario

- **💾 Base de datos**: Guarda historial de precios para análisis

## 🏗️ Arquitectura

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   WhatsApp      │────▶│   Node.js       │────▶│   FastAPI       │
│   (Usuario)     │     │   (Baileys)     │     │   (Backend)     │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                                                        │
                                                        ▼
                                                ┌─────────────────┐
                                                │   SQLite        │
                                                │   (Database)    │
                                                └─────────────────┘
```

## 📁 Estructura del Proyecto

```
supermarket-tracker/
├── backend/
│   ├── endpoints/
│   │   ├── dto/
│   │   │   ├── __init__.py
│   │   │   └── message_dto.py      # DTOs para requests/responses
│   │   ├── __init__.py
│   │   └── price_webservice.py     # Endpoints de precios
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── database.py             # Utilidades SQLite
│   │   └── ocr.py                  # Procesamiento de imágenes
│   ├── main.py                     # FastAPI app
│   └── requirements.txt
│
└── frontend/
    ├── src/
    │   ├── whatsapp_handler.ts     # Handler de WhatsApp
    │   ├── index.ts                # Entry point
    │   └── chat_cli.ts             # CLI para pruebas
    ├── package.json
    └── tsconfig.json
```

## 🚀 Instalación

### Requisitos previos

- **Python 3.10+**
- **Node.js 18+**
- **Tesseract OCR** (para reconocimiento de texto)
- **libzbar** (para códigos de barras)

### 1. Instalar Tesseract OCR

**Windows:**
```bash
# Descargar e instalar desde:
# https://github.com/UB-Mannheim/tesseract/wiki
# Instalar en: C:\Program Files\Tesseract-OCR\
```

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-spa libzbar0
```

**macOS:**
```bash
brew install tesseract tesseract-lang zbar
```

### 2. Backend (Python)

```bash
cd backend

# Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
# o: venv\Scripts\activate  # Windows

# Instalar dependencias
pip install -r requirements.txt

# Ejecutar servidor
python main.py
# o: uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

El servidor estará en: `http://localhost:8000`

### 3. Frontend (Node.js)

```bash
cd frontend

# Instalar dependencias
npm install

# Compilar TypeScript
npm run build

# Ejecutar
npm start
```

## 📱 Uso

### Conectar WhatsApp

1. Ejecuta el frontend: `npm start`
2. Escanea el código QR con WhatsApp
3. ¡Listo!

### Comandos de WhatsApp

| Comando | Descripción | Ejemplo |
|---------|-------------|---------|
| 📸 Foto | Envía foto de etiqueta | `store:exito` en caption |
| `buscar X` | Buscar producto | `buscar leche` |
| `comparar X` | Comparar precios | `comparar leche alquería` |
| `más barato X` | Mejor precio | `más barato arroz` |
| `ayuda` | Ver comandos | |

### Indicar tienda

En el caption de la foto, escribe:
```
store:la2000
store:exito
store:d1
store:ara
store:jumbo
store:olimpica
store:makro
store:carulla
```

O simplemente escribe el nombre como primera palabra:
```
exito leche entera
```

## 🔌 API Endpoints

### `POST /api/price/extract`
Extrae información de precio de una imagen.

```json
{
  "user_id": "string",
  "store": "exito",
  "mime_type": "image/jpeg",
  "file_base64": "..."
}
```

### `POST /api/price/confirm`
Confirma o corrige datos extraídos.

```json
{
  "observation_id": 123,
  "user_id": "string",
  "confirmed_price": 35350,
  "confirmed_product": "Leche Alquería 1100ml"
}
```

### `POST /api/price/search`
Busca productos por nombre.

```json
{
  "user_id": "string",
  "query": "leche",
  "limit": 10
}
```

### `POST /api/price/compare`
Compara precios entre tiendas.

```json
{
  "user_id": "string",
  "product_name": "leche alquería"
}
```

### `GET /api/price/cheapest?product=leche`
Encuentra el mejor precio.

## 🧪 Pruebas

### CLI de pruebas
```bash
cd frontend
npm run chat
```

### Probar API directamente
```bash
# Health check
curl http://localhost:8000/

# Buscar producto
curl -X POST http://localhost:8000/api/price/search \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "query": "leche"}'
```

## 📊 Base de Datos

El sistema usa SQLite con las siguientes tablas:

- **price_observations**: Observaciones de precios individuales
- **products**: Catálogo de productos (normalizado)
- **price_history**: Historial de precios para análisis

La base de datos se crea automáticamente en `./prices.db`.

## 🔧 Configuración

### Variables de entorno

**Backend:**
```bash
# No requiere configuración especial
# Tesseract path se puede ajustar en utils/ocr.py
```

**Frontend:**
```bash
# .env
BACKEND_BASE_URL=http://127.0.0.1:8000
```

## 📝 Notas

- El OCR funciona mejor con fotos claras y bien iluminadas
- Las etiquetas de Éxito, Jumbo, D1, Ara tienen formatos similares
- Los precios en promoción (etiquetas rojas) se detectan automáticamente
- Si el OCR no está seguro, pedirá confirmación

## 🐛 Solución de Problemas

### "Tesseract not found"
Asegúrate de que Tesseract esté instalado y en el PATH. En Windows, verifica la ruta en `utils/ocr.py`.

### "pyzbar import error"
Instala libzbar:
```bash
# Linux
sudo apt-get install libzbar0

# macOS
brew install zbar
```

### "WhatsApp connection failed"
1. Elimina la carpeta `auth_info_baileys`
2. Reinicia el bot
3. Escanea el QR nuevamente

## 📄 Licencia

MIT

## 🤝 Contribuir

1. Fork el repositorio
2. Crea una rama (`git checkout -b feature/mejora`)
3. Commit tus cambios (`git commit -am 'Agrega nueva característica'`)
4. Push a la rama (`git push origin feature/mejora`)
5. Crea un Pull Request
