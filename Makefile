.PHONY: test lint run matrix rag-eval db-up db-down

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

db-up:
	docker compose up -d postgres

db-down:
	docker compose down
