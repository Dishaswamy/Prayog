from typing import Literal

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import get_db
from logic import gpu_agent
from models import Node
from routers.scheduler.agent import current_node
from schemas.scheduler.agent import MetricsIn, MetricsOut, ResultOut, WorkloadChunk, WorkloadDatasetFile, WorkloadOut

# The node agent's side of an ML Flow run: fetch the work, send back what it produced. Authenticated by the
# node credential, and limited to a live allocation on that node (see logic.gpu_agent).
router = APIRouter(prefix="/agent", tags=["node agent"])

NO_STORE = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"}


@router.get("/allocations/{allocation_id}/workload", response_model=WorkloadOut)
def get_workload(allocation_id: int, node: Node = Depends(current_node), db: Session = Depends(get_db)):
    load = gpu_agent.workload(db, node.id, allocation_id)
    manifest = load.manifest
    return WorkloadOut(
        run_id=load.run.id, label=load.label, entrypoint=load.entrypoint, hyperparameters=load.run.hyperparameters,
        walltime_minutes=load.walltime_minutes, manifest_artifact_id=load.manifest_artifact_id,
        archive_sha256=manifest["archive_sha256"], archive_size_bytes=manifest["archive_size_bytes"],
        chunks=[WorkloadChunk(artifact_id=c["artifact_id"], sha256=c["sha256"], size_bytes=c["size_bytes"])
                for c in manifest["chunks"]],
        files=manifest["files"],
        dataset=[WorkloadDatasetFile(path=f.path, size_bytes=f.size_bytes, blake3=f.blake3) for f in load.dataset],
    )


@router.get("/allocations/{allocation_id}/code/{artifact_id}")
def download_code(allocation_id: int, artifact_id: str, node: Node = Depends(current_node),
                  db: Session = Depends(get_db)):
    path = gpu_agent.code_file(db, node.id, allocation_id, artifact_id)
    return FileResponse(path, media_type="application/octet-stream", headers=NO_STORE)


@router.get("/allocations/{allocation_id}/data/{blake3}")
def download_data(allocation_id: int, blake3: str, node: Node = Depends(current_node), db: Session = Depends(get_db)):
    path = gpu_agent.data_file(db, node.id, allocation_id, blake3)
    return FileResponse(path, media_type="application/octet-stream", headers=NO_STORE)


@router.post("/allocations/{allocation_id}/metrics", response_model=MetricsOut, status_code=status.HTTP_201_CREATED)
def post_metrics(allocation_id: int, body: MetricsIn, node: Node = Depends(current_node), db: Session = Depends(get_db)):
    count = gpu_agent.record_metrics(db, node.id, allocation_id, [m.model_dump() for m in body.metrics])
    return MetricsOut(recorded=count)


@router.post(
    "/allocations/{allocation_id}/results",
    response_model=ResultOut,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
)
async def post_result(
    allocation_id: int,
    request: Request,
    kind: Literal["WEIGHTS", "LOG"],
    step: int | None = None,
    best: bool = False,
    node: Node = Depends(current_node),
    db: Session = Depends(get_db),
):
    artifact = await gpu_agent.store_result(db, node.id, allocation_id, kind, request, step=step, best=best)
    return ResultOut(artifact_id=artifact.id, kind=artifact.kind, size_bytes=artifact.size_bytes)
