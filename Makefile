TF := terraform -chdir=infra

.PHONY: init fmt fmt-check validate plan corpus

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
