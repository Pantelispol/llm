CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS rag_chunks (
  chunk_id     TEXT PRIMARY KEY,
  city_id      TEXT NOT NULL,
  poi_id       TEXT NOT NULL,
  section      TEXT NOT NULL,
  text         TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  embedding    vector(384) NOT NULL
);

CREATE INDEX IF NOT EXISTS rag_chunks_city_poi ON rag_chunks (city_id, poi_id);
CREATE INDEX IF NOT EXISTS rag_chunks_embedding ON rag_chunks
  USING hnsw (embedding vector_cosine_ops);
