"""task number sequence

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Старт всегда 1: последовательность не читает настройки. registry.task_number_start
    # применяет next_task_number при первом обращении.
    op.execute("CREATE SEQUENCE task_number_seq START 1")


def downgrade() -> None:
    op.execute("DROP SEQUENCE task_number_seq")
