.PHONY: test lint run matrix rag-eval rag-ingest eval eval-full chat-demo db-up db-down

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

eval:
	LLM_MODE=replay python -m evals.llm_report
	LLM_MODE=replay python -m evals.understand_report
	LLM_MODE=replay python -m evals.router_report
	LLM_MODE=replay python -m evals.narration_report

eval-full:
	LLM_MODE=replay python -m evals.run --repetitions 2

chat-demo:
	LLM_MODE=replay WEATHER_FIXTURE=clear_day RAG_STORE=memory \
		python -m app.chat_cli --scenario assignment \
		--now 2026-09-21T12:00:00+03:00

db-up:
	docker compose up -d postgres

db-down:
	docker compose down
