FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
# --legacy-peer-deps: pre-existing peer conflict in the scaffold
# (vitest@^5 wants @types/node@^22, the scaffold pins @types/node@^20)
# -- predates Checkpoint 07, documented in docs/CHECKPOINT-07-NOTES.md
# §17 and the README. `npm ci` fails without this flag.
RUN npm ci --legacy-peer-deps
COPY . .
RUN npm run build

FROM node:20-slim
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/package.json ./package.json
COPY --from=build /app/node_modules ./node_modules
EXPOSE 3000
CMD ["npm", "start"]
