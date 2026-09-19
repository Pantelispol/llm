.PHONY: test lint run db-up db-down

test:
	pytest

lint:
	ruff check .

run:
	uvicorn app.main:app --reload

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

