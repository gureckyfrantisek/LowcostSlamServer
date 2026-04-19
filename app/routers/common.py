# The / endpoints for common actions
from fastapi import APIRouter
from app.core.common import download_all_data

router = APIRouter()

@router.post("/download-all/{project_name}")
async def download_all_route(
    project_name,
    cleanup=False
):
    return download_all_data(project_name, cleanup)