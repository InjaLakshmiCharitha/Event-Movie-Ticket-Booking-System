
"""Update booking system models safely.

Revision ID: 2423bef1df4a
Revises: 04416af6accc
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision: str = "2423bef1df4a"
down_revision: Union[str, Sequence[str], None] = "04416af6accc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing tables screens, reviews, shows and seat_category_prices
    # are intentionally not recreated.

    # BOOKING SEATS
    op.add_column(
        "booking_seats",
        sa.Column("show_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "booking_seats",
        sa.Column(
            "price",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "booking_seats",
        sa.Column("scanned_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "booking_seats",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "booking_seats",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # BOOKINGS
    # Keep legacy columns until the new application is verified.
    op.add_column(
        "bookings",
        sa.Column("booking_number", sa.String(40), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("customer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("show_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "seat_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "ticket_total",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "convenience_fee",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "gst_amount",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0.00",
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "grand_total",
            sa.Numeric(10, 2),
            nullable=True,
        ),
    )
    op.add_column(
        "bookings",
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "reminder_sent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "bookings",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Preserve values from the legacy booking fields.
    op.execute(
        """
        UPDATE bookings
        SET booking_number = booking_reference,
            customer_id = user_id,
            ticket_total = total_amount,
            grand_total = total_amount
        """
    )

    op.alter_column(
        "bookings",
        "status",
        existing_type=mysql.VARCHAR(length=20),
        type_=sa.String(length=30),
        existing_nullable=False,
    )

    op.create_index(
        "ix_bookings_booking_number",
        "bookings",
        ["booking_number"],
        unique=True,
    )

    # Do not drop the old booking columns or foreign keys here.
    # show_id and customer_id constraints will be added only after
    # the live foreign-key and table definitions are verified.

    # COUPON TABLES
    op.add_column(
        "coupon_usages",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "coupon_usages",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "coupons",
        sa.Column(
            "min_ticket_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "coupons",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "coupons",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # EVENTS
    # Retain legacy columns and existing relationships.
    op.add_column(
        "events",
        sa.Column(
            "language",
            sa.String(50),
            nullable=False,
            server_default="Not specified",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "duration_minutes",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "age_rating",
            sa.String(10),
            nullable=False,
            server_default="U",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="Draft",
        ),
    )
    op.add_column(
        "events",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # NOTIFICATIONS
    op.add_column(
        "notifications",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # PAYMENTS
    # Preserve transaction_reference until code is verified.
    op.add_column(
        "payments",
        sa.Column("transaction_id", sa.String(100), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("paid_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.execute(
        """
        UPDATE payments
        SET transaction_id = transaction_reference
        WHERE transaction_reference IS NOT NULL
        """
    )

    # REFUNDS
    # Add booking_id as nullable until existing relationships
    # have been checked.
    op.add_column(
        "refunds",
        sa.Column("booking_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "refunds",
        sa.Column("refunded_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "refunds",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # SEATS
    # Keep the existing venue_id and seat_type columns.
    # New screen fields are nullable until seats can be assigned
    # to screens and their row/category data is confirmed.
    op.add_column(
        "seats",
        sa.Column("screen_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "seats",
        sa.Column("row_label", sa.String(10), nullable=True),
    )
    op.add_column(
        "seats",
        sa.Column("category", sa.String(30), nullable=True),
    )
    op.add_column(
        "seats",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "seats",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "seats",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Keep seat_number as VARCHAR to preserve values such as A-01.

    # USERS
    # phone, is_approved, assigned_venue_id and updated_at
    # already exist in the current database; don't add them again.

    # VENUES
    op.add_column(
        "venues",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "venues",
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "venues",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # WAITLIST
    # Retain legacy columns and relationships for now.
    op.add_column(
        "waitlist_entries",
        sa.Column("customer_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "waitlist_entries",
        sa.Column("show_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "waitlist_entries",
        sa.Column(
            "seat_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "waitlist_entries",
        sa.Column(
            "preferred_category",
            sa.String(30),
            nullable=False,
            server_default="Any",
        ),
    )
    op.add_column(
        "waitlist_entries",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    op.execute(
        """
        UPDATE waitlist_entries
        SET customer_id = user_id
        """
    )


def downgrade() -> None:
    # Intentionally no destructive autogenerated downgrade.
    # Back up the database before applying this migration.
    raise NotImplementedError(
        "This migration is intentionally not reversible automatically."
    )
