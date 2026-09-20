.PHONY: test lint run matrix rag-eval rag-ingest db-up db-down

test:
	pytest

lint:
	ruff check .

run:
	uvicorn app.main:app --reload

matrix:
	python -m app.tools.travel

rag-eval:
	python -m evals.rag_report --mode bm25 --store memory

rag-ingest:
	python -m app.rag.ingest

db-up:
	docker compose up -d postgres

db-down:
	docker compose down
