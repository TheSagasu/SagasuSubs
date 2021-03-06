import asyncio
from pathlib import Path
from typing import Generator, List, Optional, cast

import httpx
import SagasuSubs.database as database
from SagasuSubs.database import dto
from SagasuSubs.log import logger
from SagasuSubs.utils import AdvanceSemaphore
from tqdm import tqdm

from . import models
from .auth import AuthTokenManager


class UploadFiles:
    def __init__(self, db_path: Path, base: str, upload_slice: int = 400):
        self.auth_data = AuthTokenManager.get_token()
        self.client = httpx.AsyncClient(
            http2=True,
            base_url=base,
            headers={"Authorization": "Bearer " + self.auth_data.token},
        )
        self.file_crud = database.FileCrud(db_path)
        self.dialog_crud = database.DialogCrud(db_path)
        self.upload_slice = upload_slice

    async def get_file(self, sha1: str) -> Optional[models.FileRead]:
        response = await self.client.get(f"/api/files/sha1/{sha1}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            return None
        else:
            logger.debug(f"File {sha1=} existed in database, skip.")
            return models.FileRead.parse_obj(response.json())

    async def upload_file(self, file: dto.FileRead) -> models.FileRead:
        assert file.series_id
        model = models.FileCreate(
            filename=file.filename,
            sha1=file.sha1,
            series_id=file.series_id,
            remark=file.path,
            user_id=self.auth_data.id,
        )
        response = await self.client.post("/api/files", json=model.dict())
        response.raise_for_status()
        logger.debug(f"File {file.filename=}, {file.sha1=} data sync to upstream.")
        return models.FileRead.parse_obj(response.json())

    async def upload_dialogs(
        self, file_id: str, dialogs: List[dto.DialogRead], slice: int = None
    ) -> List[models.DialogRead]:
        bulk_data = [
            models.DialogCreate(
                file_id=file_id,
                content=dialog.content,
                begin=dialog.begin,
                end=dialog.end,
                user_id=self.auth_data.id,
            ).dict()
            for dialog in dialogs
        ]
        slice = slice or self.upload_slice
        responses: List[models.DialogRead] = []
        for begin in range(0, len(bulk_data), slice):
            response = await self.client.post(
                "/api/dialogs/bulk",
                timeout=30,
                json={"bulk": bulk_data[begin : begin + slice]},
            )
            response.raise_for_status()
            logger.debug(
                f"Dialog for {file_id=} data sync to upstream "
                f"({begin}-{begin+slice}, total={len(bulk_data)})."
            )
            responses.extend(map(models.DialogRead.parse_obj, response.json()))

        return responses

    async def upload_subtitles(self, file: dto.FileRead):
        try:
            if await self.get_file(file.sha1):
                return
            try:
                file_response = await self.upload_file(file)
                dialog_response = await self.upload_dialogs(
                    file_response.id, file.dialogs
                )
                return dialog_response
            except httpx.HTTPStatusError as e:
                response: httpx.Response = e.response
                logger.error(
                    f"Server respond {response.status_code}, data={response.json()}"
                )
        except Exception as e:
            logger.exception(f"Exception {e} occurred during processing file:")
            raise

    async def run(self, begin: int = 0, end: int = 0, parallel: int = 2):
        sem = AdvanceSemaphore(parallel)

        with tqdm(
            iterable=self.file_crud.iterate(begin, end),
            total=self.file_crud.count(begin, end),
            colour="YELLOW",
        ) as progress:
            for file in progress:
                file = cast(dto.FileRead, file)
                progress.set_description(file.series_name)

                await sem.acquire()
                task = asyncio.create_task(self.upload_subtitles(file))
                task.add_done_callback(lambda _: sem.release())

            await sem.wait_all_finish()
            await self.client.aclose()

        return
