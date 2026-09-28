# RepoDive 프론트 이미지. **컨텍스트는 Front/ 다** (백엔드와 다르다):
#
#   docker build -f Front/Dockerfile -t repodive-front Front
#   docker run -p 80:80 repodive-front
#
# 정적 파일과 리버스 프록시를 한 컨테이너가 맡는다. 브라우저에서 보면 프론트와
# 백엔드가 같은 출처(:80)라 **CORS 가 아예 없다** — preflight 도 없다.
#
# **backend 라는 이름의 컨테이너가 같은 네트워크에 있어야 뜬다.** nginx 는 기동할 때
# upstream 이름을 풀고, 못 풀면 설정 오류로 죽는다 (nginx.conf 주석 참조).

# ── builder ──────────────────────────────────────────────────────────────
# 백엔드 이미지가 린터용으로 쓰는 것과 같은 node:22 다. 버전을 맞춰 두면
# 한쪽만 올라가서 생기는 차이가 없다.
FROM node:22-bookworm-slim AS builder

WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

# **빈 문자열이어야 한다.** api.ts 는 `import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000"`
# 인데, `??` 는 null·undefined 에만 폴백하고 빈 문자열은 그대로 쓴다. 그래서 값이 ""
# 이면 fetch 가 `/analyze` 처럼 상대 경로로 나가고 nginx 가 백엔드로 넘긴다.
# (`||` 였다면 빈 문자열이 폴백을 타서 브라우저가 127.0.0.1:8000 을 직접 불렀을 것이다.
#  이 이미지에서 그 주소는 **브라우저가 도는 호스트**를 가리키므로 백엔드가 아니다)
#
# 값이 아예 정의되지 않으면 undefined 가 되어 폴백을 탄다. 그래서 굽고 나서
# 번들에 127.0.0.1 이 없는지 확인하는 것이 검증이다.
ENV VITE_API_BASE=""

# tsc && vite build. 타입 오류가 있으면 여기서 빌드가 깨진다.
RUN npm run build

# ── runtime ──────────────────────────────────────────────────────────────
# dist/ 만 넘어온다. node_modules(수백 MB)도 소스도 최종 이미지에 없다.
# **alpine 이 아니라 alpine-slim 이다.** njs·geoip2·image-filter 모듈과 perl 이 빠진 판인데
# 이 설정은 그중 하나도 안 쓴다. 실측 93.8MB → 21.1MB (같은 검증을 둘 다 통과했다).
FROM nginx:alpine-slim

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80
