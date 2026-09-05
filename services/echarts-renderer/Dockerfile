FROM node:20-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts=false
COPY server.js ./

USER node
EXPOSE 8787
CMD ["node", "server.js"]
