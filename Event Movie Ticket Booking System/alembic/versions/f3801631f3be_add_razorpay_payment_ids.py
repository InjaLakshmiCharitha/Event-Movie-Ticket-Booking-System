
"""add razorpay payment ids

Revision ID: f3801631f3be
Revises: 1ecaba0014f5
Create Date: 2026-10-30 13:22:34.921600
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3801631f3be'
down_revision: Union[str, Sequence[str], None] = '1ecaba0014f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'payments',
        sa.Column('razorpay_order_id', sa.String(length=100), nullable=True)
    )

    op.add_column(
        'payments',
        sa.Column('razorpay_payment_id', sa.String(length=100), nullable=True)
    )

    op.create_unique_constraint(
        'uq_payments_razorpay_order_id',
        'payments',
        ['razorpay_order_id']
    )

    op.create_unique_constraint(
        'uq_payments_razorpay_payment_id',
        'payments',
        ['razorpay_payment_id']
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_payments_razorpay_payment_id',
        'payments',
        type_='unique'
    )

    op.drop_constraint(
        'uq_payments_razorpay_order_id',
        'payments',
        type_='unique'
    )

    op.drop_column('payments', 'razorpay_payment_id')
    op.drop_column('payments', 'razorpay_order_id')
