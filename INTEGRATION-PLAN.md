# Integration Plan — Elara RAG ↔ Portfolio System

> Dokumen ini mengintegrasikan service RAG **Elara** (FastAPI, Python) ke dalam sistem portfolio:
> Backend Express + Postgres (pgvector) + Dashboard + Portfolio Website.
>
> **Catatan keputusan:**
> - LLM mengikuti repo RAG as-is: **Groq `openai/gpt-oss-120b`** (generate + rewrite) dan **Google Gemini `gemini-embedding-2`** (embedding) + **`gemma-4-26b-a4b-it`** (reranker). Rencana "DeepSeek" di AGENTS.md diabaikan.
> - **Hermes Agent diabaikan** (project terpisah).
> - Vector store memakai **Postgres pgvector yang sudah ada** (bukan Supabase).

---

## 0. Status Environment (sudah dicek & diverifikasi)

| Item | Status | Catatan |
|---|---|---|
| Docker | ✅ Terpasang (v29.4.0) | Dipakai untuk dev & prod compose |
| Homebrew | ✅ Terpasang | Untuk install `uv` |
| `uv` | ✅ Terpasang (v0.12.5, via `brew install uv`) | Sudah diverifikasi |
| Python 3.11.16 | ✅ Terinstall (via `uv python install 3.11`) | Managed oleh uv, tidak mengubah Python sistem |
| Dependencies | ✅ Terinstall (via `uv sync`) | fastapi, uvicorn, groq, google-genai, asyncpg, boto3, dsb. |
| Python host | ⚠️ 3.9.6 | Tidak dipakai; repo memakai Python 3.11 dari uv |

---

## 1. Prerequisite: Install `uv` ✅ SUDAH SELESAI

`uv` adalah package manager + runner Python yang dipakai repo ini. **Sudah terinstall dan diverifikasi:**

- `uv` v0.12.5 via `brew install uv`
- Python 3.11.16 via `uv python install 3.11`
- Dependencies via `uv sync`

**Referensi install (bila di mesin lain):**
```bash
brew install uv                                   # macOS + Homebrew
# atau: curl -LsSf https://astral.sh/uv/install.sh | sh

uv --version
uv python install 3.11                            # di dalam folder RAG/
uv sync                                           # install semua dependency ke .venv
```

> Python 3.11 di-manage oleh `uv` secara isolated (di `RAG/.venv`), sehingga tidak mengubah Python sistem (3.9.6).

---

## 2. Buat API Key (Langkah ini dilakukan oleh user)

### 2.1 Gemini API Key (WAJIB — embedding + reranker)
1. Buka <https://aistudio.google.com/> (login akun Google).
2. Menu **Get API key** → **Create API key**.
3. Pilih project Google Cloud (buat baru bila perlu) → **Create API key in new project**.
4. Salin key (format `AIzaSy...`) → isi `GEMINI_API_KEY` di `.env`.

### 2.2 Groq API Key (WAJIB — LLM generate + rewrite)
1. Buka <https://console.groq.com/> (login).
2. Sidebar **API Keys** → **Create API Key** → beri nama → salin (format `gsk_...`) → isi `GROQ_API_KEY`.

> Keduanya punya free tier yang cukup untuk development.

---

## 3. ADMIN_TOKEN (Dari mana?)

`ADMIN_TOKEN` adalah **string rahasia yang kamu generate sendiri** (bukan dari provider). Dipakai untuk:
- Login admin console (username `admin`, password = nilai `ADMIN_TOKEN`), dan
- Header `X-Admin-Token` untuk endpoint `/admin/*`.

**Generate:**
```bash
openssl rand -hex 32
```
Letakkan hasilnya di `.env` sebagai `ADMIN_TOKEN=...`. Jangan di-commit.

---

## 4. Variabel Environment (file `RAG/.env`)

Salin template:
```bash
cd RAG
cp .env.example .env
```

Isi dengan nilai berikut:

```env
# Database — arahkan ke Postgres lokal (Docker), database baru "rag"
# - Dipakai dari host (npm/psql/lokal run): pakai localhost
# - Dipakai dari dalam container (compose): pakai host "postgres"
SUPABASE_DB_URL=postgresql://postgres:postgres@localhost:5432/rag

# Groq (LLM generate + rewrite) — dari §2.2
GROQ_API_KEY=gsk_...
GROQ_MODEL=openai/gpt-oss-120b
GROQ_TEMPERATURE=0.3
GROQ_MAX_TOKENS=1000

# Google AI Studio (embedding + reranker) — dari §2.1
GEMINI_API_KEY=AIzaSy...
EMBEDDING_MODEL=gemini-embedding-2
EMBEDDING_DIMENSIONS=768
RERANKER_MODEL=gemma-4-26b-a4b-it

# Cloudflare R2 (opsional — hanya dipakai saat ingest upload file)
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT=https://e8a9771c0b98462e3d146efc24944277.r2.cloudflarestorage.com
R2_BUCKET=elara-chatbot
R2_REGION=auto

# Telegram (opsional — Hermes diabaikan, biarkan kosong)
TELEGRAM_BOT_TOKEN=
OWNER_CHAT_ID=

# Admin & environment
ADMIN_TOKEN=<hasil openssl rand -hex 32>
CORS_ORIGINS=http://localhost:3000,http://localhost:3001,https://febriyann.my.id
ENVIRONMENT=development
ENABLE_DOCS=true

# RAG tuning (default repo)
RETRIEVAL_TOP_K=10
RERANK_TOP_K=3
SIMILARITY_THRESHOLD=0.6
CHUNK_SIZE=800
CHUNK_OVERLAP=0.1
```

> Catatan: variabel `SUPABASE_URL` di template tidak dipakai oleh kode — hanya `SUPABASE_DB_URL` yang dipakai (`asyncpg`). Nama variabel boleh di-rename jadi `DATABASE_URL` nanti (opsional refactor).

---

## 5. Integrasi Database (Postgres pgvector)

Tabel RAG (`documents`, `chunks`, `leads`, `intake_sessions`, `confessions`, `system_prompts`, `admin_users`) akan dibuat di database terpisah **`rag`** pada instance Postgres yang sudah ada, supaya tidak campur dengan tabel Prisma (`User`, `Project`, `Skill`) di `portfolio-data`.

### 5.1 Buat database `rag` (sekali, dev)
```bash
# dari folder Dashboard-Portfolio/backend
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U postgres -c "CREATE DATABASE rag;"
docker compose -f docker-compose.dev.yml exec postgres \
  psql -U postgres -d rag -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 5.2 Jalankan migrasi + seed
```bash
cd RAG
uv run python scripts/run_migration.py   # buat tabel + index HNSW/FTS
uv run python scripts/seed_admin.py      # seed user admin (password = ADMIN_TOKEN)
uv run python scripts/seed_kb.py         # seed contoh knowledge base (ganti dengan data kita)
```

> Catatan migrasi: script membuat role `elara` + grant. Di Postgres milik sendiri (superuser `postgres`) ini tetap jalan tapi tidak wajib. Boleh di-skip dengan menambal `run_migration.py` agar melewati blok role, atau dibiarkan (aman).

---

## 6. Menjalankan Service (Dev, tanpa Docker dulu)

```bash
cd RAG
uv run uvicorn app:app --port 8000 --reload
```
- Admin console: <http://localhost:8000/>
- Swagger docs: <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

Login admin: username `admin`, password = `ADMIN_TOKEN`.

---

## 7. Integrasi Docker Compose

Repo RAG **belum punya Dockerfile**. Untuk menjalankannya satu network dengan Postgres/API/Dashboard/Portfolio, tambahkan Dockerfile + service.

### 7.1 `RAG/Dockerfile` (baru)
```dockerfile
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

# Copy dependency files dulu agar cache layer efisien
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project

# Copy seluruh source
COPY . .

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 7.2 Service `rag` di `docker-compose.dev.yml` (tambahan)
```yaml
  rag:
    build:
      context: ../../RAG
      dockerfile: Dockerfile
    container_name: portfolio_rag_dev
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      - SUPABASE_DB_URL=postgresql://${DB_USER:-postgres}:${DB_PASSWORD:-postgres}@postgres:5432/rag
      - GROQ_API_KEY=${GROQ_API_KEY:-}
      - GEMINI_API_KEY=${GEMINI_API_KEY:-}
      - ADMIN_TOKEN=${ADMIN_TOKEN:-}
      - CORS_ORIGINS=http://localhost:3000,http://localhost:3001,https://febriyann.my.id
      - ENVIRONMENT=development
    ports:
      - "8000:8000"
    restart: unless-stopped
```

> Untuk production, tambahkan service yang sama di `docker-compose.prod.yml` dengan domain seperti `rag.febriyann.my.id` (reverse proxy) dan `ENVIRONMENT=production`.

---

## 8. Auto-sync Project → Knowledge Base (opsional tapi disarankan)

Supaya Elara bisa menjawab soal project portfolio, data project disinkron ke KB setiap kali project disimpan (mirip webhook revalidasi yang sudah ada).

- Di backend Express (`controllers/project.controller.ts`), setelah create/update/delete:
  - `POST {RAG_URL}/admin/ingest-text` dengan header `X-Admin-Token`, berisi `title`, `content` (gabung title/subtitle/description/challenge/solution/tech_stack), `source_type: "project"`, `metadata: { slug }`.
- Untuk delete: panggil endpoint delete document sesuai `document_id`/`metadata.slug`.
- Simpan `document_id` yang dikembalikan ingest di field Project (butuh kolom baru, mis. `rag_document_id`) agar update/delete bisa menargetkan dokumen yang sama.

> Variabel baru di backend: `RAG_URL` (internal, contoh `http://rag:8000`) dan `RAG_ADMIN_TOKEN` (= `ADMIN_TOKEN`).

---

## 9. Chat UI di Portfolio

- Tambahkan komponen chat minimal (contoh: floating button + panel) di `Portfolio-Website/components/web/`.
- Kirim `POST {RAG_PUBLIC_URL}/chat` dengan body `{ message, session_id, history, stream }`.
- Untuk streaming, konsumsi SSE (`stream: true`).
- Simpan `session_id` di `localStorage` agar history konsisten.
- `RAG_PUBLIC_URL` dipasang sebagai env di Vercel (`NEXT_PUBLIC_RAG_URL=https://rag.febriyann.my.id`) dan default ke `http://localhost:8000` saat dev.

---

## 10. Checklist Eksekusi (berurutan)

- [x] 1. `brew install uv` → `uv --version` (sudah: uv 0.12.5, Python 3.11.16, `uv sync` OK)
- [ ] 2. Buat `GEMINI_API_KEY` (§2.1) dan `GROQ_API_KEY` (§2.2)
- [ ] 3. Generate `ADMIN_TOKEN` (`openssl rand -hex 32`) (§3)
- [ ] 4. `cp .env.example .env` dan isi (§4)
- [ ] 5. Buat database `rag` + `CREATE EXTENSION vector` (§5.1)
- [ ] 6. `run_migration.py` + `seed_admin.py` + `seed_kb.py` (§5.2)
- [ ] 7. `uv run uvicorn app:app --port 8000 --reload` → cek `http://localhost:8000/health` (§6)
- [ ] 8. Tambah `RAG/Dockerfile` (§7.1) + service `rag` di compose (§7.2)
- [ ] 9. (Opsional) Auto-sync project → KB (§8)
- [ ] 10. Chat UI di Portfolio (§9)

**Acceptance:** `GET /health` → `200 healthy`; `POST /chat` menjawab dengan konteks KB; admin console bisa login & ingest.

---

## 11. Catatan untuk Eksekutor (termasuk DeepSeek v4 Pro)

- Jangan mengubah model LLM/Gemini/Groq default kecuali diminta; ini sudah as-intended dari repo.
- Gunakan LLM repo (Groq). Jika nanti ada permintaan ganti LLM, lakukan di `services/generate.py` dan `services/rewrite.py` (keduanya OpenAI-compatible).
- Dimensi vector **768** sudah fix di schema (`vector(768)`); jangan diubah tanpa migrasi.
- Semua perubahan DB lewat `supabase/migrations.sql` + `scripts/run_migration.py`, bukan Prisma (tabel RAG dikelola repo ini, bukan Prisma).
- Jaga `ADMIN_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY` tetap di `.env` dan tidak di-commit.
- Perubahan kode minimal; fokus pada integrasi (compose, webhook sync, chat UI) tanpa merombak logika RAG internal.
