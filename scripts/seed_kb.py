"""
scripts/seed_kb.py — Seed Knowledge Base with Riyanda's profile, services, FAQ,
and projects fetched from the portfolio public API.

Idempotent: deletes previously-seeded documents (metadata.seeded = true) first,
so it is safe to re-run.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_pool
from services.ingest import ingest_file_document

logger = logging.getLogger(__name__)

PORTFOLIO_API_URL = os.getenv("PORTFOLIO_API_URL", "http://localhost:5000")

SEED_DIR = Path(__file__).resolve().parent / "seed_kb"

STATIC_FILES = [
    ("profil.md", "profile"),
    ("layanan.md", "services"),
    ("faq.md", "faq"),
]


async def cleanup_seeded() -> int:
    """Delete previously-seeded documents (cascade removes their chunks)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "DELETE FROM documents WHERE metadata->>'seeded' = 'true' RETURNING id"
        )
    return len(rows)


def build_project_markdown(project: dict) -> str:
    lines = [f"# {project.get('title', '')}"]
    if project.get("subtitle"):
        lines.append(project["subtitle"])
    if project.get("description"):
        lines.append(project["description"])

    tech = project.get("technologies") or []
    if tech:
        lines.append("\n## Teknologi")
        lines.append(", ".join(tech))

    if project.get("challenge"):
        lines.append("\n## Challenge")
        lines.append(project["challenge"])

    if project.get("solution"):
        lines.append("\n## Solution")
        lines.append(project["solution"])

    results = project.get("results") or []
    if results:
        lines.append("\n## Hasil")
        for r in results:
            lines.append(f"- {r}")

    return "\n".join(lines)


def build_portfolio_markdown(projects: list[dict]) -> str:
    titles = [p.get("title", "") for p in projects if p.get("title")]
    lines = [
        "# Daftar Project Riyanda",
        "",
        f"Riyanda telah mengerjakan berbagai project: {', '.join(titles)}.",
        "",
    ]
    for p in projects:
        title = p.get("title", "")
        lines.append(f"## {title}")
        if p.get("subtitle"):
            lines.append(p["subtitle"])
        if p.get("description"):
            lines.append(p["description"])
        tech = p.get("technologies") or []
        if tech:
            lines.append(f"Teknologi: {', '.join(tech)}")
        lines.append("")
    return "\n".join(lines)


async def seed_static_files() -> None:
    for filename, category in STATIC_FILES:
        path = SEED_DIR / filename
        if not path.exists():
            logger.warning("Seed file not found: %s", path)
            continue
        result = await ingest_file_document(
            filename=filename,
            file_bytes=path.read_bytes(),
            source_type="manual",
            metadata={"seeded": True, "category": category},
        )
        print(f"[OK] {filename}: {result.get('chunks_created')} chunks")


async def seed_projects() -> int:
    import httpx

    url = f"{PORTFOLIO_API_URL}/api/public/projects"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("Gagal fetch projects dari %s: %s", url, e)
        return 0

    projects = data.get("data", []) or []
    count = 0
    for p in projects:
        slug = p.get("slug") or p.get("id")
        md = build_project_markdown(p)
        try:
            result = await ingest_file_document(
                filename=f"project-{slug}.md",
                file_bytes=md.encode("utf-8"),
                source_type="project",
                metadata={"seeded": True, "category": "project", "slug": slug},
            )
            count += 1
            print(f"[OK] project-{slug}.md: {result.get('chunks_created')} chunks")
        except Exception as e:
            logger.error("Gagal ingest project %s: %s", slug, e)

    # Aggregate overview document: daftar semua project (untuk pertanyaan "project apa aja")
    if projects:
        try:
            overview = build_portfolio_markdown(projects)
            result = await ingest_file_document(
                filename="portfolio.md",
                file_bytes=overview.encode("utf-8"),
                source_type="manual",
                metadata={"seeded": True, "category": "portfolio"},
            )
            print(f"[OK] portfolio.md: {result.get('chunks_created')} chunks")
        except Exception as e:
            logger.error("Gagal ingest portfolio overview: %s", e)

    return count


async def main() -> None:
    print("Mulai seeding Knowledge Base...")
    deleted = await cleanup_seeded()
    print(f"Cleanup: {deleted} dokumen seed lama dihapus")

    await seed_static_files()
    project_count = await seed_projects()

    print(f"Selesai. Total project ter-ingest: {project_count}")


if __name__ == "__main__":
    asyncio.run(main())
