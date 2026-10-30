import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.auth.security import SECRET_KEY, ALGORITHM
from app.database import SessionLocal
from app.models.models import User, Show
from app.redis_client import redis_client

router = APIRouter(tags=["Live Seat Map"])


class SeatConnectionManager:
    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = {}
        self.lock = asyncio.Lock()

    async def connect(self, show_id: int, websocket: WebSocket):
        await websocket.accept()

        async with self.lock:
            self.connections.setdefault(show_id, set()).add(websocket)

    async def disconnect(self, show_id: int, websocket: WebSocket):
        async with self.lock:
            if show_id in self.connections:
                self.connections[show_id].discard(websocket)

                if not self.connections[show_id]:
                    del self.connections[show_id]

    async def broadcast(self, show_id: int, message: dict):
        async with self.lock:
            connections = list(self.connections.get(show_id, set()))

        disconnected = []

        for websocket in connections:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)

        for websocket in disconnected:
            await self.disconnect(show_id, websocket)


manager = SeatConnectionManager()


def get_user_from_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")

        if user_id is None:
            return None

        return int(user_id)
    except (JWTError, TypeError, ValueError):
        return None


def show_exists(show_id: int) -> bool:
    db: Session = SessionLocal()

    try:
        return db.query(Show.id).filter(Show.id == show_id).first() is not None
    finally:
        db.close()


def user_exists(user_id: int) -> bool:
    db: Session = SessionLocal()

    try:
        user = db.query(User).filter(User.id == user_id).first()
        return user is not None and user.role in ("Customer", "Admin")
    finally:
        db.close()


@router.websocket("/ws/shows/{show_id}/seats")
async def live_seat_map(websocket: WebSocket, show_id: int, token: str):
    user_id = get_user_from_token(token)

    if user_id is None or not user_exists(user_id) or not show_exists(show_id):
        await websocket.close(code=1008)
        return

    await manager.connect(show_id, websocket)

    try:
        await websocket.send_json({
            "type": "connected",
            "show_id": show_id,
            "message": "Connected to live seat updates",
        })

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(show_id, websocket)
    except Exception:
        await manager.disconnect(show_id, websocket)


async def publish_seat_update(show_id: int, seat_id: int, status: str):
    await manager.broadcast(
        show_id,
        {
            "type": "seat_update",
            "show_id": show_id,
            "seat_id": seat_id,
            "status": status,
        },
    )


async def publish_seat_updates(show_id: int, seat_ids: list[int], status: str):
    await manager.broadcast(
        show_id,
        {
            "type": "seats_update",
            "show_id": show_id,
            "seat_ids": seat_ids,
            "status": status,
        },
    )
