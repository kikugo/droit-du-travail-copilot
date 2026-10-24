# Droit du Travail Copilot

**FR.** Agent de questions-réponses sur le Code du travail, construit sur Azure AI Foundry. Il répond en français, cite les articles avec leur lien Légifrance et refuse de répondre quand les sources ne couvrent pas la question. Ce n'est pas un avis juridique.

**EN.** A French labour-law question-answering agent on Azure AI Foundry: Azure AI Search for hybrid retrieval, Microsoft Agent Framework for the agent, Terraform for the infrastructure. Personal project, work in progress.

## Status

Work in progress. Results tables will be generated from `results/*.json` once the eval exists.

## Infrastructure

Everything on Azure is in `infra/` (Terraform, azurerm provider, local state): a resource group, a Foundry account with one project and two model deployments (chat and `text-embedding-3-small`), Azure AI Search on the Free tier, and Log Analytics with Application Insights.

```bash
make validate                               # fmt + validate, no Azure calls
make plan                                   # writes infra/tfplan
terraform -chdir=infra apply tfplan
terraform -chdir=infra output
terraform -chdir=infra destroy
```
