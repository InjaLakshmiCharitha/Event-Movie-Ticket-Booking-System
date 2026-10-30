from io import BytesIO
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth.dependencies import get_current_user
from app.models.models import (
    User,
    Event,
    Show,
    Screen,
    Seat,
    Booking,
    BookingSeat,
    Payment,
    Refund,
)
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment

router = APIRouter(prefix="/reports", tags=["Reports"])


def require_role(user: User, *roles):
    if user.role not in roles:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to access this report"
        )


def date_range(start_date: date, end_date: date):
    if start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="Start date cannot be after end date"
        )

    start_datetime = datetime.combine(start_date, time.min)
    end_datetime = datetime.combine(
        end_date + timedelta(days=1),
        time.min
    )

    return start_datetime, end_datetime



@router.get("/admin-dashboard")
def admin_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin")

    today = date.today()
    start_datetime, end_datetime = date_range(today, today)

    today_bookings = db.query(func.count(Booking.id)).filter(
        Booking.created_at >= start_datetime,
        Booking.created_at < end_datetime
    ).scalar() or 0

    today_revenue = db.query(
        func.coalesce(func.sum(Payment.amount), 0)
    ).filter(
        Payment.status == "Success",
        Payment.paid_at >= start_datetime,
        Payment.paid_at < end_datetime
    ).scalar() or Decimal("0")

    today_refunds = db.query(
        func.coalesce(func.sum(Refund.amount), 0)
    ).filter(
        Refund.status == "Success",
        Refund.refunded_at >= start_datetime,
        Refund.refunded_at < end_datetime
    ).scalar() or Decimal("0")

    active_events = db.query(func.count(Event.id)).filter(
        Event.status == "Published"
    ).scalar() or 0

    tickets_scanned = db.query(
        func.count(BookingSeat.id)
    ).filter(
        BookingSeat.scanned_at >= start_datetime,
        BookingSeat.scanned_at < end_datetime
    ).scalar() or 0

    return {
        "date": str(today),
        "today_bookings": today_bookings,
        "today_revenue": float(today_revenue),
        "today_refunds": float(today_refunds),
        "active_events": active_events,
        "tickets_scanned": tickets_scanned
    }


@router.get("/event-sales")
def event_sales_report(
    start_date: date,
    end_date: date,
    event_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin", "Organizer")

    start_datetime, end_datetime = date_range(
        start_date,
        end_date
    )

    query = (
        db.query(
            Event.id.label("event_id"),
            Event.title.label("event_title"),
            func.count(Booking.id).label("total_bookings"),
            func.coalesce(
                func.sum(Booking.seat_count), 0
            ).label("tickets_sold"),
            func.coalesce(
                func.sum(Booking.grand_total), 0
            ).label("total_sales")
        )
        .join(Show, Show.event_id == Event.id)
        .join(Booking, Booking.show_id == Show.id)
        .filter(
            Booking.status == "Confirmed",
            Booking.created_at >= start_datetime,
            Booking.created_at < end_datetime
        )
    )

    if current_user.role == "Organizer":
        query = query.filter(
            Event.organizer_id == current_user.id
        )

    if event_id is not None:
        query = query.filter(Event.id == event_id)

    results = (
        query
        .group_by(Event.id, Event.title)
        .order_by(func.sum(Booking.grand_total).desc())
        .all()
    )

    return {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "reports": [
            {
                "event_id": row.event_id,
                "event_title": row.event_title,
                "total_bookings": row.total_bookings,
                "tickets_sold": row.tickets_sold,
                "total_sales": float(row.total_sales)
            }
            for row in results
        ]
    }


@router.get("/show-occupancy/{show_id}")
def show_occupancy_report(
    show_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin", "Organizer")

    show = db.query(Show).filter(
        Show.id == show_id
    ).first()

    if not show:
        raise HTTPException(
            status_code=404,
            detail="Show not found"
        )

    event = db.query(Event).filter(
        Event.id == show.event_id
    ).first()

    if current_user.role == "Organizer":
        if event.organizer_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="You can view only your own event reports"
            )

    total_seats = db.query(func.count(Seat.id)).filter(
        Seat.screen_id == show.screen_id,
        Seat.is_active.is_(True)
    ).scalar() or 0

    booked_seats = db.query(
        func.count(BookingSeat.id)
    ).join(
        Booking,
        BookingSeat.booking_id == Booking.id
    ).filter(
        BookingSeat.show_id == show_id,
        Booking.status == "Confirmed"
    ).scalar() or 0

    category_rows = (
        db.query(
            Seat.category,
            func.count(BookingSeat.id).label("tickets_sold")
        )
        .join(
            Booking,
            BookingSeat.booking_id == Booking.id
        )
        .join(
            Seat,
            BookingSeat.seat_id == Seat.id
        )
        .filter(
            BookingSeat.show_id == show_id,
            Booking.status == "Confirmed"
        )
        .group_by(Seat.category)
        .all()
    )

    category_capacity = (
        db.query(
            Seat.category,
            func.count(Seat.id).label("total_seats")
        )
        .filter(
            Seat.screen_id == show.screen_id,
            Seat.is_active.is_(True)
        )
        .group_by(Seat.category)
        .all()
    )

    sold_by_category = {
        row.category: row.tickets_sold
        for row in category_rows
    }

    category_report = []

    for row in category_capacity:
        sold = sold_by_category.get(row.category, 0)
        capacity = row.total_seats

        category_report.append({
            "category": row.category,
            "total_seats": capacity,
            "tickets_sold": sold,
            "available_seats": capacity - sold,
            "occupancy_percentage": round(
                (sold / capacity) * 100, 2
            ) if capacity else 0
        })

    return {
        "show_id": show_id,
        "event_title": event.title,
        "total_seats": total_seats,
        "tickets_sold": booked_seats,
        "available_seats": total_seats - booked_seats,
        "occupancy_percentage": round(
            (booked_seats / total_seats) * 100, 2
        ) if total_seats else 0,
        "category_wise_occupancy": category_report
    }


@router.get("/top-events")
def top_events_report(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin")

    start_datetime, end_datetime = date_range(
        start_date,
        end_date
    )

    results = (
        db.query(
            Event.id.label("event_id"),
            Event.title.label("event_title"),
            func.count(BookingSeat.id).label("tickets_sold")
        )
        .join(Show, Show.event_id == Event.id)
        .join(BookingSeat, BookingSeat.show_id == Show.id)
        .join(Booking, BookingSeat.booking_id == Booking.id)
        .filter(
            Booking.status == "Confirmed",
            Booking.created_at >= start_datetime,
            Booking.created_at < end_datetime
        )
        .group_by(Event.id, Event.title)
        .order_by(
            func.count(BookingSeat.id).desc()
        )
        .limit(10)
        .all()
    )

    return {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "top_events": [
            {
                "event_id": row.event_id,
                "event_title": row.event_title,
                "tickets_sold": row.tickets_sold
            }
            for row in results
        ]
    }


@router.get("/refund-summary")
def refund_summary_report(
    start_date: date,
    end_date: date,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin")

    start_datetime, end_datetime = date_range(
        start_date,
        end_date
    )

    results = (
        db.query(
            Refund.reason.label("reason"),
            func.count(Refund.id).label("refund_count"),
            func.coalesce(
                func.sum(Refund.amount), 0
            ).label("total_amount")
        )
        .filter(
            Refund.status == "Success",
            Refund.refunded_at >= start_datetime,
            Refund.refunded_at < end_datetime
        )
        .group_by(Refund.reason)
        .order_by(func.sum(Refund.amount).desc())
        .all()
    )

    return {
        "start_date": str(start_date),
        "end_date": str(end_date),
        "refund_summary": [
            {
                "reason": row.reason,
                "refund_count": row.refund_count,
                "total_amount": float(row.total_amount)
            }
            for row in results
        ]
    }


@router.get("/event-sales/export")
def export_event_sales_excel(
    start_date: date,
    end_date: date,
    event_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    require_role(current_user, "Admin", "Organizer")

    start_datetime, end_datetime = date_range(
        start_date,
        end_date
    )

    query = (
        db.query(
            Event.id.label("event_id"),
            Event.title.label("event_title"),
            func.count(Booking.id).label("total_bookings"),
            func.coalesce(
                func.sum(Booking.seat_count), 0
            ).label("tickets_sold"),
            func.coalesce(
                func.sum(Booking.grand_total), 0
            ).label("total_sales")
        )
        .join(Show, Show.event_id == Event.id)
        .join(Booking, Booking.show_id == Show.id)
        .filter(
            Booking.status == "Confirmed",
            Booking.created_at >= start_datetime,
            Booking.created_at < end_datetime
        )
    )

    # Organizers can export only their own events.
    if current_user.role == "Organizer":
        query = query.filter(
            Event.organizer_id == current_user.id
        )

    if event_id is not None:
        query = query.filter(Event.id == event_id)

    results = (
        query
        .group_by(Event.id, Event.title)
        .order_by(func.sum(Booking.grand_total).desc())
        .all()
    )

    # Create Excel workbook.
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Event Sales"

    # Report title.
    worksheet.merge_cells("A1:E1")
    worksheet["A1"] = "EVENT AND MOVIE TICKET SALES REPORT"
    worksheet["A1"].font = Font(
        bold=True,
        size=14,
        color="FFFFFF"
    )
    worksheet["A1"].fill = PatternFill(
        start_color="1F4E78",
        end_color="1F4E78",
        fill_type="solid"
    )
    worksheet["A1"].alignment = Alignment(
        horizontal="center"
    )

    worksheet.append([])
    worksheet.append([
        "Start Date",
        str(start_date),
        "End Date",
        str(end_date)
    ])

    worksheet.append([])

    # Column headings.
    headers = [
        "Event ID",
        "Event Name",
        "Total Bookings",
        "Tickets Sold",
        "Total Sales (INR)"
    ]

    worksheet.append(headers)

    for cell in worksheet[5]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(
            start_color="4472C4",
            end_color="4472C4",
            fill_type="solid"
        )
        cell.alignment = Alignment(
            horizontal="center"
        )

    # Add report data.
    for row in results:
        worksheet.append([
            row.event_id,
            row.event_title,
            row.total_bookings,
            row.tickets_sold,
            float(row.total_sales)
        ])

    # Add total row.
    last_row = worksheet.max_row + 1

    worksheet.cell(last_row, 2, "TOTAL")
    worksheet.cell(
        last_row,
        3,
        sum(row.total_bookings for row in results)
    )
    worksheet.cell(
        last_row,
        4,
        sum(row.tickets_sold for row in results)
    )
    worksheet.cell(
        last_row,
        5,
        sum(
            (row.total_sales for row in results),
            Decimal("0")
        )
    )

    for cell in worksheet[last_row]:
        cell.font = Font(bold=True)

    # Adjust column widths.
    column_widths = {
        "A": 15,
        "B": 35,
        "C": 20,
        "D": 18,
        "E": 22
    }

    for column, width in column_widths.items():
        worksheet.column_dimensions[column].width = width

    # Prepare Excel file in memory.
    output = BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = (
        f"event_sales_report_{start_date}_{end_date}.xlsx"
    )

    return StreamingResponse(
        output,
        media_type=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        }
    )