
"""Repair the existing booking database schema.

Revision ID: d1e78783b275
Revises: 2423bef1df4a
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.database import Base
import app.models.models  # Load all model definitions


revision: str = "d1e78783b275"
down_revision: Union[str, Sequence[str], None] = "2423bef1df4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)

    # Keep the existing users table and its user records.
    # Rebuild the other tables only if they contain no records.
    business_tables = [
        table
        for table in Base.metadata.sorted_tables
        if table.name != "users"
    ]

    existing_tables = set(inspector.get_table_names())

    # Safety check: never drop business data.
    tables_with_data = []

    for table in business_tables:
        if table.name in existing_tables:
            count = connection.execute(
                sa.text(
                    f"SELECT COUNT(*) FROM `{table.name}`"
                )
            ).scalar_one()

            if count > 0:
                tables_with_data.append(
                    f"{table.name} ({count} rows)"
                )

    if tables_with_data:
        raise RuntimeError(
            "Schema repair stopped because these tables contain data: "
            + ", ".join(tables_with_data)
            + ". No tables were dropped. Back up the database and "
            "review the migration before continuing."
        )

    # Remove existing user-to-venue foreign keys temporarily,
    # because the venues table will be rebuilt.
    for fk in inspector.get_foreign_keys("users"):
        if fk.get("referred_table") in {
            table.name for table in business_tables
        }:
            if fk.get("name"):
                op.drop_constraint(
                    fk["name"],
                    "users",
                    type_="foreignkey",
                )

    # Existing legacy schemas differ significantly from the models.
    # Disable FK checks only while removing the verified-empty tables.
    connection.execute(sa.text("SET FOREIGN_KEY_CHECKS = 0"))

    try:
        Base.metadata.drop_all(
            bind=connection,
            tables=business_tables,
            checkfirst=True,
        )
    finally:
        connection.execute(
            sa.text("SET FOREIGN_KEY_CHECKS = 1")
        )

    # Recreate business tables using the current SQLAlchemy models.
    Base.metadata.create_all(
        bind=connection,
        tables=business_tables,
        checkfirst=True,
    )

    # Restore the optional assigned-venue relationship on users.
    inspector = sa.inspect(connection)
    user_fks = inspector.get_foreign_keys("users")

    has_venue_fk = any(
        fk.get("referred_table") == "venues"
        and fk.get("constrained_columns") == ["assigned_venue_id"]
        for fk in user_fks
    )

    if not has_venue_fk:
        op.create_foreign_key(
            "fk_users_assigned_venue_id_venues",
            "users",
            "venues",
            ["assigned_venue_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    raise RuntimeError(
        "This schema repair cannot automatically restore the legacy schema."
    )

