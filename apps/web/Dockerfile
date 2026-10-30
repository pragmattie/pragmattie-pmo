# Development image: runs the Vite dev server on port 5173.
FROM node:24-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

USER node
EXPOSE 5173
CMD ["npm", "run", "dev"]
