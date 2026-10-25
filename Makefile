TF := terraform -chdir=infra

.PHONY: init fmt fmt-check validate plan corpus env embed index query ask smoke serve

init:
	$(TF) init

fmt:
	$(TF) fmt -recursive

fmt-check:
	$(TF) fmt -check -recursive

validate:
	$(TF) init -backend=false -input=false
	$(TF) validate

# apply and destroy stay manual: terraform -chdir=infra apply tfplan
plan:
	TF_VAR_subscription_id=$$(az account show --query id -o tsv) $(TF) plan -out=tfplan

# downloads LEGI and KALI once into data/raw, then writes data/articles.jsonl
corpus:
	uv run python -m ingest

# endpoints and names only (no keys), read by retrieval/config.py
env:
	@terraform -chdir=infra output -json | python3 -c 'import json,sys; o=json.load(sys.stdin); print("AZURE_SEARCH_ENDPOINT=" + o["search_endpoint"]["value"]); print("AZURE_OPENAI_ENDPOINT=" + o["openai_endpoint"]["value"]); print("EMBEDDING_DEPLOYMENT=" + o["embedding_deployment"]["value"]); print("CHAT_DEPLOYMENT=" + o["chat_deployment"]["value"]); print("FOUNDRY_PROJECT_ENDPOINT=" + o["project_endpoint"]["value"])' > .env
	@echo "wrote .env"

# costs tokens (about 3.5M for the full corpus); resumable
embed:
	uv run python -m retrieval embed

index:
	uv run python -m retrieval build $(if $(SCOPE),--scope $(SCOPE)) $(if $(COMPRESSION),--compression $(COMPRESSION)) $(if $(RECREATE),--recreate)
	uv run python -m retrieval check-semantic

MODE ?= hybrid
query:
	uv run python -m retrieval query "$(Q)" --mode $(MODE)

# agent (each question costs a few thousand gpt-5.4-mini tokens)
ask:
	uv run python -m copilot "$(Q)"

smoke:
	uv run python -m copilot smoke

serve:
	uv run uvicorn copilot.api:app --reload --port 8000
