FROM node:20-alpine

WORKDIR /app

# Copiar archivos de dependencias
COPY package.json package-lock.json ./

# Instalar dependencias
RUN npm ci

# Copiar código fuente
COPY . .

# Compilar TypeScript
RUN npm run build 2>/dev/null || true

ENV NODE_ENV=production

# Comando por defecto
CMD ["node", "dist/index.js"]
