import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router
from app.routers.venue import router as venue_router
from app.routers.event import router as event_router
from app.routers.seat_lock import router as seat_lock_router
from app.routers.seat_websocket import router as seat_websocket_router
from app.routers.booking import router as booking_router
from app.routers.payment import router as payment_router
from app.routers.ticket import router as ticket_router
from app.routers.cancellation import router as cancellation_router
from app.routers.reports import router as reports_router
from app.routers.coupon import router as coupon_router

from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Event & Movie Ticket Booking System")

    start_scheduler()

    try:
        yield
    finally:
        stop_scheduler()
        logger.info("Event & Movie Ticket Booking System stopped")


app = FastAPI(
    title="Event & Movie Ticket Booking System",
    description="Backend API for event and movie ticket booking",
    version="1.0.0",
    lifespan=lifespan,
)


app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(venue_router)
app.include_router(event_router)
app.include_router(seat_lock_router)
app.include_router(seat_websocket_router)
app.include_router(booking_router)
app.include_router(payment_router)
app.include_router(ticket_router)
app.include_router(cancellation_router)
app.include_router(reports_router)
app.include_router(coupon_router)

@app.get("/")
def home():
    return {
        "message": "Event & Movie Ticket Booking System API is running"
    }

