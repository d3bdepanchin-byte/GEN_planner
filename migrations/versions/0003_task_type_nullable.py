"""task.type nullable: тип может отсутствовать в источнике

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('task', 'type', nullable=True)


def downgrade() -> None:
    op.execute("UPDATE task SET type = '' WHERE type IS NULL")
    op.alter_column('task', 'type', nullable=False)
