# cbioportal-slide-viewer — Operations Runbook

## Overview

`cbioportal-slide-viewer` is a FastAPI tile server that streams IMPACT pathology whole-slide images (SVS) directly from Dell ECS S3 (`mskmind-bkt`) via `tiffslide`. Slide and patient metadata come from Databricks Unity Catalog. The service is deployed at `https://slides.cbioportal.org`.

---

## Architecture

```
Browser
  └─ HTTPS → slides.cbioportal.org
                └─ nginx ingress (rate-limiting, TLS termination)
                      └─ slide-viewer pods (×2, FastAPI)
                            ├─ Redis          — tile + patient cache
                            ├─ Dell ECS S3    — SVS files (mskmind-bkt)
                            └─ Databricks SQL — slide/patient metadata
```

---

## Deployment

### Prerequisites

1. **Kubernetes secret** — must exist before ArgoCD first sync:
   ```bash
   kubectl create secret generic slide-viewer-secrets \
     --from-literal=REEF_AWS_KEY=<ecs-access-key> \
     --from-literal=REEF_AWS_SECRET=<ecs-secret-key> \
     --from-literal=DATABRICKS_HOST=https://msk-mode-prod.cloud.databricks.com \
     --from-literal=DATABRICKS_TOKEN=<pat> \
     --from-literal=ANNOTATION_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/cbioportal_annotations?sslmode=require
   ```

   The current dev Lakebase endpoint for annotation storage is:
   - host: `ep-proud-hat-d23z7mxx.database.us-east-1.cloud.databricks.com`
   - database: `cbioportal_annotations`
   - login role: `cbioportal_api`

2. **GitHub Actions secret** — for GitOps image-tag pinning:
   - `K8S_DEPLOY_TOKEN`: PAT with `repo` write access to `msk-mind/knowledgesystems-k8s-deployment`

3. **DNS** — CNAME `slides.cbioportal.org` → cluster ingress ALB (infra team).

4. **ECR repository** — `cbioportal-slide-viewer` must exist in account `203403084713`; OIDC role `github-actions-ecr-push` must have `ecr:PutImage` on it.

### ArgoCD Sync

```bash
# Apply the ArgoCD Application (one-time):
kubectl apply -f argocd/aws/203403084713/clusters/cbioportal-prod/apps/argocd/slide-viewer.yaml

# ArgoCD will auto-sync the slide-viewer/ directory.
# Check sync status:
argocd app get slide-viewer
```

### CORS

Edit `configmap.yaml` and add the MSK internal cBioPortal hostname to `CORS_ORIGINS`:
```yaml
CORS_ORIGINS: "https://cbioportal.org,https://www.cbioportal.org,https://genie.cbioportal.org,https://cbioportal.mskcc.org"
```

Annotation auth settings also live in the slide-viewer `configmap.yaml`:
```yaml
KEYCLOAK_JWKS_URL: "https://keycloak.cbioportal.mskcc.org/auth/realms/msk/protocol/openid-connect/certs"
ANNOTATION_AUTH_ENABLED: "true"
```

---

## CI/CD

Pushes to `main` or version tags trigger `.github/workflows/docker-publish.yml`:

1. Builds and pushes the Docker image to ECR with tags `sha-<7char>`, `latest`, and semver (on tags).
2. **`update-k8s-manifests` job** — commits the pinned `sha-<7char>` tag into `deployment.yaml` in `knowledgesystems-k8s-deployment`. ArgoCD detects the diff and rolls out automatically.

### Databricks bundle split

- Root `databricks.yml` manages the nightly WSI summary job.
- `databricks/lakebase/` contains the separate Lakebase bundle for annotation
  OLTP storage. Keep that provisioning isolated from the batch SQL job bundle.

---

## Adding a New Study

When a cBioPortal study dataset is updated or a new IMPACT study is added, run the migration tool:

```bash
# From the cbioportal-slide-viewer repo root:
python tools/generate_resource_patient.py \
    --study-dir /path/to/private/automation_tool_datasets/<study_id> \
    --base-url https://slides.cbioportal.org

# Then remove legacy DSA resource files if present:
rm -f <study_dir>/data_resource_sample.txt
rm -f <study_dir>/meta_resource_sample.txt
```

The tool:
- Reads patient IDs from `data_clinical_sample.txt`
- Queries Databricks (`slide_inventory` JOIN) to find patients with servable slides
- Writes `data_resource_patient.txt`, `meta_resource_patient.txt`, `data_resource_definition.txt`, `meta_resource_definition.txt`

After running, open a PR in the `private` repo and reload the study in cBioPortal.

**Batch migration** (all MSK studies):
```bash
bash tools/migrate_all_studies.sh
```

---

## Health Checks

```bash
# Liveness (pods should return 200):
curl https://slides.cbioportal.org/health

# Patient metadata (requires a valid PATIENT_ID):
curl "https://slides.cbioportal.org/patient/P-0000001" | jq .patient_id

# Tile endpoint:
curl -o /dev/null -w "%{http_code}" \
  "https://slides.cbioportal.org/tiles/<slide_id>/zxy/0/0/0"
```

---

## Monitoring

| Signal | Where |
|---|---|
| Pod logs | `kubectl logs -l app=slide-viewer --tail=100 -f` |
| Redis memory | `kubectl exec deploy/slide-viewer-redis -- redis-cli info memory` |
| Tile cache hit rate | `kubectl exec deploy/slide-viewer-redis -- redis-cli info stats \| grep keyspace` |
| Ingress rate-limit drops | Nginx ingress controller logs (`kubectl logs -n ingress-nginx`) |

---

## Common Issues

### 502 on `/patient/{id}`
- Databricks warehouse may be asleep (cold start ~30 s) — retry once.
- Check `DATABRICKS_TOKEN` is not expired: `kubectl get secret slide-viewer-secrets -o yaml`
- Check pod logs for `"Databricks query failed"` or `"timed out"`.

### 404 on tile endpoint
- Slide may not have a `slide_inventory` row for the given `image_id`.
- Verify: query `cdsi_eng_phi.pdm_base_tables.slide_inventory WHERE image_id = '<id>'` in Databricks.
- The ECS bucket key may have moved — check `slide_inventory.path`.

### Redis OOM
- Default `maxmemory=8gb` with `allkeys-lru` — Redis will evict old tiles automatically.
- If eviction rate is very high, consider increasing `SLIDE_VIEWER_REDIS_MAXMEMORY` in `redis.yaml`.

### Ingress rate-limit (HTTP 429)
- A single viewer loading a large WSI can briefly exceed 100 req/s.
- Increase `limit-rps` in `ingress.yaml` or whitelist the origin in nginx config.

---

## Secrets Rotation

### ECS credentials
```bash
kubectl create secret generic slide-viewer-secrets \
  --from-literal=REEF_AWS_KEY=<new-key> \
  --from-literal=REEF_AWS_SECRET=<new-secret> \
  --from-literal=DATABRICKS_HOST=https://msk-mode-prod.cloud.databricks.com \
  --from-literal=DATABRICKS_TOKEN=<pat> \
  --from-literal=ANNOTATION_DATABASE_URL=postgresql://<user>:<password>@<host>:5432/cbioportal_annotations?sslmode=require \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart deployment/slide-viewer
```

### Databricks PAT
1. Generate a new PAT in Databricks UI (Settings → Developer → Access Tokens).
2. Update the secret as above with the new `DATABRICKS_TOKEN`.
3. Restart the deployment.

---

## Rollback

ArgoCD allows instant rollback to any prior synced revision:

```bash
argocd app history slide-viewer
argocd app rollback slide-viewer <revision>
```

Or directly edit `deployment.yaml` to a previous `sha-<7char>` tag and push.
