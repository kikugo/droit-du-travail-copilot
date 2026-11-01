TF := terraform -chdir=infra

.PHONY: init fmt fmt-check validate plan corpus env embed index query ask smoke serve draft review eval-retrieval eval-agent report gate baseline corpus-download corpus-release docker-build docker-run gh-vars wake

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
	@terraform -chdir=infra output -json | python3 -c 'import json,sys; o=json.load(sys.stdin); print("AZURE_SEARCH_ENDPOINT=" + o["search_endpoint"]["value"]); print("AZURE_OPENAI_ENDPOINT=" + o["openai_endpoint"]["value"]); print("EMBEDDING_DEPLOYMENT=" + o["embedding_deployment"]["value"]); print("CHAT_DEPLOYMENT=" + o["chat_deployment"]["value"]); print("JUDGE_DEPLOYMENT=" + o["judge_deployment"]["value"]) if "judge_deployment" in o else None; print("FOUNDRY_PROJECT_ENDPOINT=" + o["project_endpoint"]["value"])' > .env
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

# eval (drafting, the agent run and the judge cost tokens; everything is cached)
draft:
	uv run python -m eval.draft_questions

review:
	uv run python -m eval.review

eval-retrieval:
	uv run python -m eval.run_retrieval --label $(or $(LABEL),free) $(if $(ENDPOINT),--search-endpoint $(ENDPOINT)) $(if $(INDEX),--index $(INDEX))

eval-agent:
	uv run python -m eval.run_agent $(if $(SUBSET),--subset $(SUBSET))

report:
	uv run python -m eval.report

# CI gate: smoke20 (20 live questions, about $0.10) compared row by row with results/baseline.json
gate:
	uv run python -m eval.run_agent --subset smoke20 --no-judge
	uv run python -m eval.gate

# run on main, then commit results/baseline.json
baseline:
	uv run python -m eval.run_agent --subset smoke20 --no-judge
	uv run python -m eval.gate --write-baseline

# data/articles.jsonl for CI and the image, from the corpus-v1 release (checked against ingest/corpus-v1.sha256)
CORPUS_TAG := corpus-v1
corpus-download:
	mkdir -p data
	gh release download $(CORPUS_TAG) --pattern articles.jsonl.gz --dir data --clobber
	cd data && shasum -a 256 -c ../ingest/$(CORPUS_TAG).sha256
	gunzip -f data/articles.jsonl.gz

# writes the archive and its hash; uploading it is a separate, manual step
corpus-release:
	gzip -9 -n -c data/articles.jsonl > data/articles.jsonl.gz
	cd data && shasum -a 256 articles.jsonl.gz > ../ingest/$(CORPUS_TAG).sha256
	@echo "upload with: gh release create $(CORPUS_TAG) data/articles.jsonl.gz --title 'Corpus v1' --notes 'data/articles.jsonl (LEGI + KALI, Licence Ouverte 2.0)'"

docker-build:
	docker build -t ddt-copilot:local .

# checks the page and /health; /ask needs the app's managed identity, so try it with `make serve`
docker-run:
	docker run --rm -p 8000:8000 --env-file .env ddt-copilot:local

# the non-secret GitHub repo variables CI and deploy read, from terraform outputs
gh-vars:
	@terraform -chdir=infra output -json | python3 -c 'import json,sys; o=json.load(sys.stdin); v={"AZURE_CLIENT_ID":"ci_client_id","AZURE_TENANT_ID":"tenant_id","AZURE_SUBSCRIPTION_ID":"subscription_id","FOUNDRY_PROJECT_ENDPOINT":"project_endpoint","AZURE_OPENAI_ENDPOINT":"openai_endpoint","AZURE_SEARCH_ENDPOINT":"search_endpoint","CHAT_DEPLOYMENT":"chat_deployment","EMBEDDING_DEPLOYMENT":"embedding_deployment","CONTAINER_APP_NAME":"container_app_name","RESOURCE_GROUP":"resource_group_name","APP_URL":"app_url"}; print("\n".join(k + "=" + o[t]["value"] for k, t in v.items()))' > .gh-vars.tmp
	gh variable set --env-file .gh-vars.tmp
	rm .gh-vars.tmp

# the app scales to zero; run this about 30 s before a demo so the first visitor doesn't wait
wake:
	@url=$$(terraform -chdir=infra output -raw app_url); \
	for i in $$(seq 1 12); do curl -fsS -m 30 "$$url/health" >/dev/null && echo "awake: $$url" && exit 0; sleep 5; done; \
	echo "no answer from $$url/health" && exit 1
