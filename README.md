# work-memory — AI uchun uzoq muddatli xotira (MCP server)

FastAPI + MCP (streamable HTTP) + Postgres/pgvector + S3. Claude Code sessiyalari va Telegram bot bitta
xotiradan foydalanadi. Embedding lokal model (fastembed, ONNX), tashqi API token yo'q.

## Qatlamlar
| Qatlam | Nima | Qayerda |
|---|---|---|
| Source of truth | har memory = bitta `.md` (JSON front matter + body), niqoblangan | S3 `memories/<kind>/<id>.md` |
| Indeks | full-text (`tsvector`) + vektor (`pgvector`, HNSW), hybrid qidiruv (RRF) | Postgres |
| Lug'at (vault) | niqoblanadigan so'zlar va tokenlar, qiymatlari Fernet bilan shifrlangan | Postgres + S3 `vault/` |
| Common | git repo/umumiy kod snapshot'i, faqat o'qish | S3 `common/<collection>/...` + Postgres chunk'lar |

Postgres yo'qolsa: yangi baza → `POST /api/admin/restore` → hammasi S3'dan qayta quriladi.

## Xavfsizlik
- **Secret'lar** (parol, key, token, connection string, SAS, JWT, Telegram token, private key) saqlashdan oldin
  butunlay o'chiriladi: `[REDACTED_SECRET:<tur>]`. Qaytarib bo'lmaydi.
- **Ismlar, kompaniya so'zlari, email, id'lar** → `PERSON_3f9a0c12be` kabi token. Haqiqiy qiymat faqat shifrlangan
  holda. S3 yoki baza dump'i o'g'irlansa ham ismlar ko'rinmaydi. Kalitlar (`PSEUDO_*`) faqat env'da.
- Email avtomatik topiladi; ism/kompaniya lug'atga qo'shiladi (`pseudo_add_term` yoki `/api/admin/terms`).
  `substring` rejimi so'z ichida ham topadi (masalan kompaniya nomi identifikator ichida).
- Kirish: `MCP_TOKEN` (AI mijozlar), `ADMIN_TOKEN` (upload/restore). `/health` ochiq.
- Common: server hech qachon git ishlatmaydi (clone/pull/commit/push yo'q). Snapshot'ni admin yuklaydi.

## API doc (odamlar uchun)
`https://<domain>/docs` → **Authorize** → token. Memory'lar (`/api/memories`, `/api/memories/search`, `/api/tickets/{ticket}`),
statistika, common fayllar (`/api/common`, `/read`, `/search`, `/grep`), lug'at (`/api/terms`) va AI ko'radigan MCP tool'lar
(`/api/tools`). Admin endpoint'lar (`/api/admin/...`) faqat admin token bilan.

## MCP tool'lar
`task_context`, `memory_search`, `memory_save`, `memory_get`, `memory_update`, `memory_archive`, `memory_recent`,
`close_task`, `memory_stats`, `pseudo_add_term`, `pseudo_list_terms`, `pseudo_suggest_terms`, `common_list`,
`common_search`, `common_grep`, `common_read`.

Embedding faqat ma'no bor joyda: memory'lar va hujjatlar (`.md`, `.txt`). Kod embedding qilinmaydi — nomi bo'yicha
(full-text + grep) topiladi; kod "qanday ishlaydi" degan bilim `architecture` tegli memory sifatida saqlanadi.
Qidiruv: kalit so'z va ma'no ballari (har biri eng yaxshisiga nisbatan 0..1) 50/50 birlashtiriladi. Qachon nima ishlatish server `instructions` ichida yozilgan (Claude ulanganda o'zi oladi).

## Lokal ishga tushirish (Docker)
```bash
python scripts/gen_keys.py > .env      # bir marta; PSEUDO_* kalitlarni yo'qotmang
docker compose up -d --build
curl localhost:8000/health
./scripts/test.sh                      # testlar alohida vaqtinchalik stack'da (haqiqiy bazaga tegmaydi)
```

Claude Code'ga ulash:
```bash
claude mcp add --transport http work-memory http://localhost:8000/mcp --header "Authorization: Bearer $MCP_TOKEN"
```

Common snapshot yuklash (faqat o'qiydi, git'ga tegmaydi):
```bash
ADMIN_TOKEN=... python scripts/upload_common.py ~/path/to/repo synapse-repo --url http://localhost:8000
```

Shell'dan tool chaqirish (MCP ulanmagan agent yoki tekshirish uchun):
```bash
python scripts/mem.py tools
python scripts/mem.py memory_search '{"query": "backfill window"}'
```

Eslatmalarni yuklash (faqat .md/.txt, repo nusxasiz):
```bash
python scripts/upload_common.py ~/notes notes --exclude repo-copy drafts --only-ext .md .txt
```

Hamma term'ni saqlangan narsalarga qayta qo'llash: `POST /api/admin/remask` (admin token; fonda ishlaydi, `job_id` qaytaradi → `GET /api/admin/jobs/{id}`).

Lug'atga ko'p so'z qo'shish:
```bash
curl -X POST localhost:8000/api/admin/terms -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '[{"kind":"PERSON","value":"Ism Familiya"},{"kind":"ORG","value":"kompaniya","match_mode":"substring"}]'
```

## Railway
1. Yangi project → GitHub repo (yoki `railway up`) shu papkadan; Dockerfile avtomatik topiladi.
2. Postgres qo'shish: **pgvector** template'li Postgres (oddiy Postgres'da `vector` extension bo'lmasligi mumkin).
3. S3: Railway Bucket yoki istalgan S3 (Cloudflare R2, AWS). `S3_ENDPOINT_URL`, `S3_BUCKET`, kalitlar, `S3_REGION`.
4. Variables: `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `PSEUDO_HMAC_KEY`, `PSEUDO_ENC_KEY`, `MCP_TOKEN`, `ADMIN_TOKEN`
   (lokaldagidan boshqa, `gen_keys.py` bilan). Healthcheck path: `/health`.
5. Mijozlarda URL: `https://<app>.up.railway.app/mcp`.

## Telegram bot (`bot/`) — 24/7, subscription bilan
Bot har xabarni `claude -p` ga beradi (Claude obunasi, API key emas). Bilim manbai — shu MCP xotira; fayl yo'q.
Bash yo'q; fayl o'qish/yozish faqat `/data/work` ichida; token saqlaydigan fayllar va `/proc` taqiqlangan.

Railway'da ikkinchi servis:
1. Lokalda bir marta: `claude setup-token` → chiqqan tokenni faqat Railway'ga qo'ying (chatga emas).
2. New → GitHub Repo → shu repo, **Root Directory: `bot`**, region `sfo`.
3. Volume: mount path `/data` (sessiyalar, state, ish papkasi).
4. Variables:
   ```
   TELEGRAM_TOKEN=<bot token>
   PAIR_CODE=<ulanish kodi>
   CLAUDE_CODE_OAUTH_TOKEN=<setup-token natijasi>
   MEMORY_MCP_URL=https://<memory domain>/mcp
   MCP_TOKEN=${{<memory servis nomi>.MCP_TOKEN}}
   BOT_MODEL=opus
   ```
5. Restart policy: Always. Domain kerak emas.
6. Lokal botni to'xtating (bir token bilan ikki bot ishlasa Telegram 409 beradi).
7. Telegram'da avval ustoz, keyin shogird `PAIR_CODE` ni yuboradi (birinchi ulangan = egasi). `/new` — yangi suhbat.

Bloklar: `<<<XABAR Ism ... XABAR>>>` kontaktga xabar, `<<<FAYL nom.md [Ism] ... FAYL>>>` fayl yuborish.

## Arzonroq variant
Bitta kichik VPS (2 vCPU / 4 GB, oyiga ~$5) da shu `docker-compose.yml` (dev volume qatorisiz) + keyin bot ham shu yerda.
S3 uchun Cloudflare R2 (10 GB bepul). Railway qulayroq (deploy, backup, TLS o'zi), lekin ~$8–10 (bot bilan ~$13–15).

## Tuzilma
```
app/main.py         FastAPI, auth, admin REST, MCP mount
app/rest.py         odamlar uchun o'qish REST API (/docs)
app/mcp_tools.py    MCP tool'lar va AI uchun instructions
app/memories.py     save/update/archive/search/task_context, S3 + Postgres tranzaksiya
app/common.py       snapshot upload (tar.gz), list/read/search/grep, notebook JSON → kod
app/pseudonym.py    secret o'chirish, niqoblash/ochish
app/vault.py        lug'at va tokenlarni saqlash, S3 backup, restore
app/embeddings.py   lokal embedding (multilingual MiniLM, 384, 2 thread)
app/privacy.py      niqoblanmagan ismlarni topish (pseudo_suggest_terms)
scripts/            gen_keys, upload_common, mem (CLI client), test.sh
app/migrations/     SQL sxema
```
