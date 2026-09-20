from app.rag.ingest import chunk_document, ingest_corpus
from app.rag.models import ContentDoc
from app.tools.catalog import CatalogRepository


def test_ingestion_is_deterministic() -> None:
    first = ingest_corpus()
    second = ingest_corpus()

    assert [(chunk.chunk_id, chunk.content_hash) for chunk in first] == [
        (chunk.chunk_id, chunk.content_hash) for chunk in second
    ]


def test_chunk_ids_are_unique() -> None:
    chunk_ids = [chunk.chunk_id for chunk in ingest_corpus()]
    assert len(chunk_ids) == len(set(chunk_ids))


def test_long_section_splits_at_paragraph_boundary() -> None:
    first_paragraph = " ".join(["alpha"] * 120) + "."
    second_paragraph = " ".join(["beta"] * 120) + "."
    document = ContentDoc(
        poi_id="rotunda",
        title="Rotunda",
        aliases=["Rotunda of Galerius"],
        source_urls=[],
        lang="en",
        confidence="draft",
        needs_review=True,
        body=f"## Long section\n\n{first_paragraph}\n\n{second_paragraph}",
    )
    poi = CatalogRepository().get("rotunda")

    chunks = chunk_document(document, poi)

    assert [chunk.chunk_id for chunk in chunks] == [
        "rotunda#aliases",
        "rotunda#long-section-1",
        "rotunda#long-section-2",
    ]
    assert chunks[1].text == first_paragraph
    assert chunks[2].text == second_paragraph


def test_alias_chunk_contains_greek_and_english_names() -> None:
    alias = next(chunk for chunk in ingest_corpus() if chunk.chunk_id == "rotunda#aliases")

    assert alias.is_alias_chunk is True
    assert "Rotunda" in alias.text
    assert "Ροτόντα" in alias.text
