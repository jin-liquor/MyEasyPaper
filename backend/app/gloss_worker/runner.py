"""Legacy polling client that refuses account AI workloads."""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from ..services.ai_client import AIError
from ..services.gloss import GLOSS_CONCURRENCY

logger = logging.getLogger(__name__)


def _concurrency() -> int:
    raw = os.getenv("EASYPAPER_GLOSS_CONCURRENCY", str(GLOSS_CONCURRENCY))
    try:
        value = int(raw)
    except ValueError:
        value = GLOSS_CONCURRENCY
    return max(1, min(value, 16))


class GlossWorkerRunner:
    def __init__(self, *, base_url: str, token: str, worker_label: str, poll_seconds: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.worker_label = worker_label[:120]
        self.poll_seconds = max(1.0, poll_seconds)
        self._headers = {"Authorization": f"Bearer {self.token}"}

    async def run_forever(self) -> None:
        logger.info(
            "Gloss worker started base_url=%s label=%s concurrency=%s", self.base_url, self.worker_label, _concurrency()
        )
        async with httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(600.0, connect=30.0)) as client:
            while True:
                try:
                    handled = await self._poll_once(client)
                except httpx.HTTPError as exc:
                    logger.warning("Gloss worker request failed: exception_type=%s", type(exc).__name__)
                    handled = False
                if not handled:
                    await asyncio.sleep(self.poll_seconds)

    async def _poll_once(self, client: httpx.AsyncClient) -> bool:
        response = await client.post(
            "/api/gloss-worker/jobs/claim",
            headers=self._headers,
            json={"worker_label": self.worker_label},
        )
        response.raise_for_status()
        payload = response.json()
        job = payload.get("job")
        if not job:
            return False
        claim_token = job["claim_token"]
        try:
            await self._process_job(client, claim_token)
        except AIError as exc:
            logger.error("Gloss worker AI job failed: exception_type=%s", type(exc).__name__)
            await self._fail(client, claim_token, "本地点读准备失败，请重试。")
        except Exception as exc:
            logger.error("Gloss worker job failed: exception_type=%s", type(exc).__name__)
            await self._fail(client, claim_token, "本地点读准备失败，请重试。")
        return True

    async def _process_job(self, client: httpx.AsyncClient, claim_token: str) -> None:
        raise AIError("账户 AI 任务须在服务器执行。")

    async def _fail(self, client: httpx.AsyncClient, claim_token: str, message: str) -> None:
        try:
            await client.post(
                f"/api/gloss-worker/jobs/{claim_token}/fail",
                headers=self._headers,
                json={"message": message[:500]},
            )
        except httpx.HTTPError as exc:
            logger.error("Failed to report gloss worker failure: exception_type=%s", type(exc).__name__)


def run_worker(*, base_url: str, token: str, worker_label: str, poll_seconds: float) -> int:
    if not token.strip():
        raise ValueError("GLOSS_WORKER_TOKEN 或 --token 不能为空")
    runner = GlossWorkerRunner(
        base_url=base_url,
        token=token,
        worker_label=worker_label,
        poll_seconds=poll_seconds,
    )
    asyncio.run(runner.run_forever())
    return 0
